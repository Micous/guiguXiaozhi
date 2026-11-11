"""组合根 —— 对齐参考项目 mini_harness/application.py。

把各层组件显式装配在一起（对应 Spring 的 @Configuration + @Bean）。
想换成自己的实现，只改这里就行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from xiaozhi.config import Settings, get_settings
from xiaozhi.config_guard import ProductionConfigError, validate_production_settings
from xiaozhi.core.tool import Tool
from xiaozhi.memory.context import ContextAssembler, rule_based_summary
from xiaozhi.memory.store import (
    ConversationStore,
    FileConversationStore,
    MongoConversationStore,
    ResilientConversationStore,
)
from xiaozhi.models.ollama import OllamaChatModel, OllamaEmbeddingModel
from xiaozhi.ops.audit import AuditLogger
from xiaozhi.ops.lifecycle import GracefulShutdown, HealthChecker, RateLimiter
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.profile.service import ProfileService
from xiaozhi.profile.store import FactCipher, MySQLProfileRepository, ProfileDisabledError
from xiaozhi.profile.injector import ProfileInjector
from xiaozhi.profile.vector import MilvusProfileVectorStore
from xiaozhi.reliability.logging import configure_logging
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig
from xiaozhi.reliability.trace_store import TraceStore, set_default_store
from xiaozhi.repositories.appointment_repo import (
    AppointmentRepository,
    InMemoryAppointmentRepository,
    MySQLAppointmentRepository,
)
from xiaozhi.repositories.session_repo import MySQLSessionRepository
from xiaozhi.retrieval.rag import RagService
from xiaozhi.retrieval.vector_store import MilvusVectorStore
from xiaozhi.runtime.engine import AgentEngine
from xiaozhi.security.auth import TokenService
from xiaozhi.security.permissions import PermissionDecisionEngine
from xiaozhi.security.secure_executor import SecureToolExecutor
from xiaozhi.security.session_access import SessionAccess
from xiaozhi.service.impl.VectorStoreDoctorService import VectorStoreDoctorService
from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.slot_service import MockSlotService, SlotService
from xiaozhi.tools.appointment_tools import (
    BookAppointmentTool,
    CancelAppointmentTool,
    QueryDoctorsTool,
    QuerySlotTool,
)
from xiaozhi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass
class Application:
    """应用容器"""

    settings: Settings
    chat_model: OllamaChatModel
    embedding_model: OllamaEmbeddingModel
    vector_store: MilvusVectorStore
    rag: RagService
    doctor_service: DoctorService
    slot_service: SlotService
    repository: AppointmentRepository
    conversation_store: ConversationStore
    tool_registry: ToolRegistry
    secure_executor: SecureToolExecutor
    engine: AgentEngine
    trace_store: TraceStore
    metrics: MetricsCollector
    audit: AuditLogger
    health: HealthChecker
    rate_limiter: RateLimiter
    shutdown: GracefulShutdown
    # P2 患者画像（灰度：profile_enabled=false 时为 None）
    profile_service: Optional[ProfileService] = None
    # P1 认证与会话归属（灰度：auth_enabled=false 时为 None）
    token_service: Optional[TokenService] = None
    session_access: Optional[SessionAccess] = None

    async def aclose(self) -> None:
        """优雅停机：释放连接"""
        await self.chat_model.aclose()
        await self.embedding_model.aclose()
        await self.conversation_store.close()
        self.audit.close()


def load_system_prompt(doctor_service=None) -> str:
    """读取系统提示词（与 Java 版同一份内容），并替换模板变量。

    Java 版用的是 LangChain4j 的 PromptTemplate，``{{current_date}}`` 会自动替换；
    这里自己替换，否则模型会看到字面量 ``{{current_date}}``。

    2026-11-10 第八轮事故：用户问"有内科吗"被意图层当成知识问答（注入 RAG），
    模型拿通用医学知识答"内科是一个较大的科室，包含了多个亚专科"——
    本院根本没有"内科"这个科室。工具描述里的科室清单只有模型主动看工具时才可见，
    system prompt 必须带权威名单，模型回答科室存在性问题时才有依据。
    """
    from datetime import date

    path = PROMPT_DIR / "xiaozhi-prompt.txt"
    if not path.exists():
        text = "你是硅谷小智，北京协和医院的智能客服与医疗顾问。"
    else:
        text = path.read_text(encoding="utf-8")
    text = text.replace("{{current_date}}", date.today().isoformat())

    if doctor_service is not None:
        try:
            departments = sorted(doctor_service.list_departments() or [])
        except Exception:
            departments = []
        if departments:
            text += (
                "\n\n9、本院开设科室（权威名单，仅当用户询问“本院有没有某科室”时才以此为准回答）："
                + "、".join(departments)
                + "。名单里没有的科室（如泛称的“内科”）直接说明本院未开设并推荐最接近的一个，"
                "一两句话即可，不要主动罗列症状分诊；用户没有问起科室时，绝不要主动提及这份名单。"
                "若历史中的旧回答与名单矛盾，以名单为准，等用户再次问到相关话题时再修正。"
                # 2026-11-10 第十轮事故：预约流程中问"外科"，RAG 检索命中通用
                # 医学资料，模型据此推荐"普外科、心脏外科"等名单外科室，
                # 用户照着预约被工具校验拦截。必须明确名单压过检索内容。
                "特别注意：知识库检索到的科室信息可能与本名单不一致，"
                "两者冲突时一律以本名单为准——检索内容里出现但名单里没有的科室"
                "（如“心脏外科”“普外科”）一律视为本院未开设，禁止向用户推荐或确认预约。"
            )
    return text


def _build_slot_service(settings: Settings, doctor_service: DoctorService) -> SlotService:
    """按配置装配号源适配器（组合根唯一装配点）。

    目前只有 MockSlotService。Phase 3b 实现 HisSlotService 后在此加分支，
    并同步把适配器名登记进 config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS，
    production 门禁才会放行。填了未实现的名字要在任何外部连接前就报错，
    不能等到工具层才发现号源是假的。
    """
    impl = settings.slot_service_impl.strip().lower()
    if impl == "mock":
        return MockSlotService(doctor_service)
    raise ProductionConfigError(
        f"号源适配器 {settings.slot_service_impl!r} 尚未实现："
        "请在 xiaozhi/service/ 下实现 HisSlotService 并在组合根注册后再启动。"
    )


async def get_application(settings: Optional[Settings] = None) -> Application:
    """装配整个应用"""
    settings = settings or get_settings()
    # 生产门禁：必须在任何外部连接（Ollama / Milvus / RAG 灌库）之前执行，
    # 否则「拒绝启动」会退化成「连了一圈依赖才失败」，且 /health 可能已经 ok。
    validate_production_settings(settings)
    configure_logging(settings.log_level, json_output=settings.json_logs)

    # ---- 认证与会话归属（P1，灰度默认关）----
    # 开启但配置/存储不就绪时**直接抛错**，不静默降级：画像模块曾因初始化失败
    # 被「禁用+告警」吞掉而假健康（见《踩坑与优化记录》13.1）。
    token_service: Optional[TokenService] = None
    session_access: Optional[SessionAccess] = None
    if settings.auth_enabled:
        token_service = TokenService(
            secret_key=settings.auth_secret_key,
            ttl_seconds=settings.auth_token_ttl_seconds,
            issuer=settings.auth_issuer,
            bootstrap_secret=settings.auth_bootstrap_secret,
            ops_subjects=settings.auth_ops_subjects,
        )
        session_repo = MySQLSessionRepository(
            settings.mysql_host,
            settings.mysql_port,
            settings.mysql_user,
            settings.mysql_password,
            settings.mysql_database,
        )
        session_repo.ensure_table()
        session_access = SessionAccess(session_repo)
        logger.info("认证已启用（issuer=%s，会话归属表已就绪）", settings.auth_issuer)

    # ---- 追踪 ----
    trace_store = TraceStore(
        max_traces=settings.trace_max, persist_dir=settings.trace_persist_dir
    )
    trace_store.load_from_disk()
    set_default_store(trace_store)

    # ---- 运维组件 ----
    metrics = MetricsCollector()
    audit = AuditLogger(settings.audit_log_path)
    rate_limiter = RateLimiter(
        settings.rate_limit_max_requests, settings.rate_limit_window_seconds
    )
    shutdown = GracefulShutdown(settings.drain_timeout)

    # ---- 模型 ----
    chat_model = OllamaChatModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_chat_model,
        temperature=settings.ollama_temperature,
        num_ctx=settings.ollama_num_ctx,
        timeout=settings.ollama_timeout,
    )
    embedding_model = OllamaEmbeddingModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        timeout=settings.ollama_timeout,
    )

    # ---- 向量库 + RAG ----
    vector_store = MilvusVectorStore(
        uri=settings.milvus_uri,
        alias=settings.milvus_alias,
        metric_type=settings.milvus_metric_type,
        index_type=settings.milvus_index_type,
    )
    rag = RagService(
        vector_store=vector_store,
        embedding_model=embedding_model,
        knowledge_dir=Path(settings.knowledge_dir),
        manifest_path=Path(
            settings.manifest_path
            or (Path(settings.trace_persist_dir).parent / f"rag_manifest_{settings.milvus_alias}.json")
        ),
        dense_top_k=settings.rag_dense_top_k,
        sparse_top_k=settings.rag_sparse_top_k,
        dense_min_score=settings.rag_dense_min_score,
        excluded_files=settings.rag_excluded_files,
    )
    stats = await rag.bootstrap()
    logger.info(
        "RAG 就绪：%s 块 / %s 文件，更新 %s 个，降级=%s",
        stats.chunks,
        stats.files,
        stats.updated_files,
        stats.degraded,
    )

    # ---- 院内系统 ----
    doctor_service = VectorStoreDoctorService(
        retrieve_all_chunks=vector_store.all_texts,
        roster_file=settings.rag_excluded_files[0] if settings.rag_excluded_files else "doctors.md",
    )
    slot_service = _build_slot_service(settings, doctor_service)

    # ---- 仓储 ----
    repository = MySQLAppointmentRepository(
        settings.mysql_host,
        settings.mysql_port,
        settings.mysql_user,
        settings.mysql_password,
        settings.mysql_database,
    )

    # ---- 记忆 ----
    conversation_store = ResilientConversationStore(
        primary=MongoConversationStore(
            settings.mongo_uri, settings.mongo_database
        ),
        fallback=FileConversationStore(str(Path(settings.trace_persist_dir).parent / "memory")),
    )

    # ---- 工具 ----
    tool_registry = ToolRegistry()
    # 工具顺序影响模型的选择偏好（实测：写操作的工具放前面，
    # 模型优先选它 —— 查号源放前面时模型会先查号源然后就"口头确认"而不真正下单）。
    tool_registry.register_all(
        [
            BookAppointmentTool(repository, slot_service, doctor_service),
            QuerySlotTool(slot_service, doctor_service),
            QueryDoctorsTool(doctor_service),
            CancelAppointmentTool(repository),
        ]
    )

    # ---- 健康检查 ----
    health = HealthChecker()

    async def _probe_model() -> dict:
        try:
            resp = await chat_model.client.get(
                f"{chat_model.base_url}/api/tags", timeout=3.0
            )
            return {"ok": resp.status_code == 200, "name": chat_model.model}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _probe_milvus() -> dict:
        # 只查元数据可达性。不要用 count()：它内部会 flush（Milvus 的重操作，
        # 实测 >2s），拿来做存活探针会把 /ready 拖慢甚至超时。
        client = vector_store.client
        has_alias = client.has_collection(vector_store.alias)
        versioned = [
            name
            for name in client.list_collections()
            if name.startswith(f"{vector_store.alias}_v")
        ]
        return {
            "ok": bool(has_alias or versioned),
            "alias": vector_store.alias,
            "collections": versioned[:3],
        }

    def _probe_mysql() -> dict:
        return {"ok": repository.list_all(limit=1) is not None}

    health.register("model", _probe_model)
    health.register("milvus", _probe_milvus)
    health.register("mysql", _probe_mysql)

    # ---- 权限 ----
    permission_engine = PermissionDecisionEngine.from_yaml(Path(settings.permissions_file))
    # 未在 YAML 显式配置的工具按风险等级推导默认策略（显式配置优先）——
    # 新增只读工具不再需要手动改 permissions.yaml
    for definition in tool_registry.list_definitions():
        permission_engine.register_tool_default(definition.name, definition.risk_level)
    secure_executor = SecureToolExecutor(
        permission_engine,
        no_ui_policy=settings.permission_no_ui,
        audit_logger=audit,
    )

    # ---- 患者画像（P2，灰度开关默认关）----
    # profile_enabled=false：所有组件保持 None，engine 行为与一期完全一致（红线）。
    # ProfileDisabledError（缺 PROFILE_SECRET_KEY 等）：禁用 + 告警，不阻止启动。
    profile_injector = None
    profile_service: Optional[ProfileService] = None
    if settings.profile_enabled:
        try:
            cipher = FactCipher(settings.profile_secret_key)
            profile_repository = MySQLProfileRepository(
                settings.mysql_host,
                settings.mysql_port,
                settings.mysql_user,
                settings.mysql_password,
                settings.mysql_database,
            )
            profile_vector = MilvusProfileVectorStore(
                milvus_uri=settings.milvus_uri,
                embed_fn=embedding_model.embed,
                dimension=len(await embedding_model.embed("dim")),
            )

            async def _extract_llm(prompt: str) -> str:
                turn = await chat_model.chat([{"role": "user", "content": prompt}])
                return turn.text or ""

            profile_service = ProfileService(
                repository=profile_repository,
                cipher=cipher,
                llm_fn=_extract_llm,
                vector_store=profile_vector,
                extract_interval_turns=settings.profile_extract_interval_turns,
            )
            profile_injector = ProfileInjector(
                repository=profile_repository,
                vector_store=profile_vector,
            )
            logger.info("患者画像已启用（profile_enabled=true）")
        except ProfileDisabledError as exc:
            logger.warning("患者画像禁用：%s", exc)
        except Exception as exc:
            logger.warning("患者画像初始化失败（禁用）：%s: %s", type(exc).__name__, exc)

    # ---- 运行时 ----
    engine = AgentEngine(
        chat_model=chat_model,
        tool_registry=tool_registry,
        secure_executor=secure_executor,
        conversation_store=conversation_store,
        context_assembler=ContextAssembler(
            token_budget=settings.memory_token_budget,
            summarizer=rule_based_summary,
        ),
        system_prompt=load_system_prompt(doctor_service),
        retriever=rag,
        max_turns=settings.max_turns,
        max_tool_calls=settings.max_tool_calls,
        metrics=metrics,
        retry_config=RetryConfig(
            max_attempts=settings.ollama_max_retries + 1,
            base_delay=settings.retry_backoff_base,
        ),
        circuit_breaker=CircuitBreaker(
            "ollama",
            failure_threshold=settings.circuit_failure_threshold,
            recovery_timeout=settings.circuit_recovery_timeout,
        ),
        request_timeout=settings.request_timeout,
        tool_failure_threshold=settings.tool_failure_threshold,
        profile_injector=profile_injector,
        profile_service=profile_service,
    )

    return Application(
        settings=settings,
        chat_model=chat_model,
        embedding_model=embedding_model,
        vector_store=vector_store,
        rag=rag,
        doctor_service=doctor_service,
        slot_service=slot_service,
        repository=repository,
        conversation_store=conversation_store,
        tool_registry=tool_registry,
        secure_executor=secure_executor,
        engine=engine,
        trace_store=trace_store,
        metrics=metrics,
        audit=audit,
        health=health,
        rate_limiter=rate_limiter,
        shutdown=shutdown,
        profile_service=profile_service,
        token_service=token_service,
        session_access=session_access,
    )
