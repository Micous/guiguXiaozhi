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
from xiaozhi.ops.audit import AuditLogger, MongoAuditSink
from xiaozhi.ops.lifecycle import GracefulShutdown, HealthChecker, RateLimiter
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.ops.redis_limiter import RateLimitBackendError, build_rate_limiter
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
from xiaozhi.repositories.approval_repo import MySQLApprovalRepository
from xiaozhi.repositories.session_repo import MySQLSessionRepository
from xiaozhi.repositories.task_repo import MySQLTaskRepository
from xiaozhi.retrieval.doc_meta import collect_meta_issues
from xiaozhi.retrieval.rag import RagService
from xiaozhi.retrieval.vector_store import MilvusVectorStore
from xiaozhi.runtime.engine import AgentEngine
from xiaozhi.runtime.task_tracker import TaskTracker
from xiaozhi.security.approval_service import ApprovalService
from xiaozhi.security.auth import TokenService
from xiaozhi.security.permissions import PermissionDecisionEngine
from xiaozhi.security.pii import build_pii_codec
from xiaozhi.security.secure_executor import SecureToolExecutor
from xiaozhi.security.session_access import SessionAccess
from xiaozhi.service.impl.VectorStoreDoctorService import VectorStoreDoctorService
from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.his_client import HisClient
from xiaozhi.service.his_slot_service import HisSlotService
from xiaozhi.service.local_his import LocalHisServer
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
    # P2 审批闭环（灰度：approval_enabled=false 时为 None）
    approval_service: Optional[ApprovalService] = None
    # 本地 HIS 替身（slot_service_impl=local_his 时非 None）
    local_his_server: Optional[LocalHisServer] = None

    async def aclose(self) -> None:
        """优雅停机：释放连接"""
        await self.chat_model.aclose()
        await self.embedding_model.aclose()
        await self.conversation_store.close()
        if self.local_his_server is not None:
            self.local_his_server.stop()
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


def _build_slot_service(
    settings: Settings,
    doctor_service: DoctorService,
    *,
    local_his_server=None,
) -> SlotService:
    """按配置装配号源适配器（组合根唯一装配点）。

    ``mock``：本地模拟（内网演示用，一步式，无预占/退号语义）。
    ``local_his``：**本地 HIS 替身**（推荐给开发/联调）—— 行为完整的
      预占/确认/释放/退号/查单，号源**真的会被扣与归还**，零外部进程
      （内嵌在应用进程的后台线程里），状态落本地 JSON 文件。
    ``his``：真实医院 HIS —— 需要 ``HIS_API_BASE_URL`` 等配置。

    ⚠️ 关键红线：**只有真正连上真实 HIS 并通过端到端验收之后**，
    才允许把适配器名登记进 ``config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS``，
    production 门禁才会放行。适配层代码写完 ≠ 可以宣称"真实挂号可用"。

    填了未实现的名字要在任何外部连接前就报错，不能等到工具层才发现
    号源是假的。
    """
    impl = settings.slot_service_impl.strip().lower()
    if impl == "mock":
        return MockSlotService(doctor_service)
    if impl == "local_his":
        # 本地 HIS 替身：内嵌启动，号源从**本院真实医生排班**生成
        # （保证"号源里有的医生"与"查询专家工具返回的医生"一致，
        #  不会出现用户约不到但能查到的割裂）。
        if local_his_server is None:
            raise ProductionConfigError(
                "slot_service_impl='local_his' 但本地 HIS 服务未装配："
                "请检查组合根的装配顺序。"
            )
        roster = []
        for dept in doctor_service.list_departments() or []:
            for doctor in doctor_service.list_doctors(dept) or []:
                roster.append({"department": doctor.department, "name": doctor.name})
        count = local_his_server.seed_from_roster(
            roster, limit_num=settings.local_his_limit_num
        )
        base_url = local_his_server.start()
        logger.info(
            "号源适配器：本地 HIS 替身（%s，排班 %d 条，限号 %d）",
            base_url,
            count,
            settings.local_his_limit_num,
        )
        client = HisClient(
            base_url,
            token=settings.his_api_token or "local-his",
            timeout=settings.his_api_timeout,
        )
        return HisSlotService(client, org_id=settings.his_org_id)
    if impl == "his":
        if not settings.his_api_base_url.strip():
            raise ProductionConfigError(
                "slot_service_impl='his' 但未配置 HIS_API_BASE_URL："
                "请设置医院 HIS 网关地址后重启。"
            )
        client = HisClient(
            settings.his_api_base_url,
            token=settings.his_api_token,
            timeout=settings.his_api_timeout,
            paths={
                "hold": settings.his_hold_path,
                "confirm": settings.his_confirm_path,
                "release": settings.his_release_path,
                "cancel": settings.his_cancel_path,
            }
            or None,
        )
        logger.info(
            "号源适配器：真实 HIS（base=%s）", settings.his_api_base_url
        )
        return HisSlotService(client, org_id=settings.his_org_id)
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
            audience=settings.auth_audience,
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

    # ---- Phase 5（P1-02）：任务生命周期持久化（灰度默认关）----
    # 开启时：建表（失败直接抛错，不静默降级）+ 启动时执行崩溃恢复扫描。
    # 恢复扫描只做标记（RUNNING→INTERRUPTED、STARTED→UNKNOWN），**绝不重试**：
    # 副作用是否发生必须查业务事实（预约表唯一约束 + 原子查重写入兜底）。
    task_tracker = None
    if settings.task_tracking_enabled:
        task_repo = MySQLTaskRepository(
            settings.mysql_host,
            settings.mysql_port,
            settings.mysql_user,
            settings.mysql_password,
            settings.mysql_database,
        )
        task_repo.ensure_tables()
        report = task_repo.recover_stale(settings.task_stale_after_seconds)
        if report.interrupted_executions or report.unknown_tool_executions:
            logger.warning(
                "崩溃恢复扫描：中断执行 %d 个、待核实工具执行 %d 个（UNKNOWN 须查业务事实，勿重试）",
                len(report.interrupted_executions),
                len(report.unknown_tool_executions),
            )
        task_tracker = TaskTracker(task_repo, model_id=settings.ollama_chat_model)
        logger.info("任务生命周期持久化已启用（task_tracking_enabled=true）")

    # ---- 运维组件 ----
    metrics = MetricsCollector()
    # 审计：fail-closed + 可选集中存储（Phase 6）。
    # mongo sink 启动先 ping：连不上直接抛错，不静默降级成本地文件
    # （降级后集中库里查不到记录，运维会以为"没有操作发生过"）。
    audit_sinks = []
    sink_choice = (settings.audit_sink or "file").strip().lower()
    if sink_choice in ("mongo", "file,mongo", "file+mongo", "both"):
        mongo_sink = MongoAuditSink(
            settings.mongo_uri,
            database=settings.audit_mongo_database,
            collection=settings.audit_mongo_collection,
        )
        mongo_sink.ping()
        audit_sinks.append(mongo_sink)
        logger.info(
            "审计集中存储已启用：Mongo %s.%s",
            settings.audit_mongo_database,
            settings.audit_mongo_collection,
        )
    elif sink_choice not in ("", "file"):
        raise ProductionConfigError(
            f"audit_sink={settings.audit_sink!r} 无效：只能是 file / mongo / file,mongo"
        )
    audit = AuditLogger(
        None if sink_choice.startswith("mongo") else settings.audit_log_path,
        fail_closed=settings.audit_fail_closed,
        sinks=audit_sinks,
        on_write_error=(lambda detail: logger.error("审计写入失败：%s", detail)),
    )
    if settings.audit_fail_closed:
        logger.info("审计 fail-closed 已启用：审计不可用时工具执行会被拒绝")
    try:
        rate_limiter = build_rate_limiter(settings)
    except RateLimitBackendError as exc:
        # 配了 Redis 却连不上 → 拒绝启动。静默退回进程内会让"多实例额度一致"
        # 这个前提无人察觉地失效（等于额度被实例数放大）。
        raise ProductionConfigError(f"限流后端不可用，拒绝启动：{exc}") from exc
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
    # Phase 6：灌库（写向量集合）只允许**单写者**执行。
    #   rag_ingest_on_startup=true（默认）：沿用一期行为，启动即增量灌库，
    #     适合单实例开发/内网演示；
    #   false：只读挂载（连现有集合 + 建稀疏索引），灌库交给
    #     `scripts/ingest_knowledge.py`。多实例部署必须用这个模式 ——
    #     N 个实例并发写同一集合会出现半成品索引与清单互相覆盖。
    if settings.rag_ingest_on_startup:
        stats = await rag.bootstrap()
        logger.info(
            "RAG 就绪（启动期灌库）：%s 块 / %s 文件，更新 %s 个，降级=%s",
            stats.chunks,
            stats.files,
            stats.updated_files,
            stats.degraded,
        )
    else:
        stats = await rag.mount_readonly()
        if stats.degraded:
            logger.error("RAG 只读挂载失败（知识检索将不可用）：%s", stats.error)
        else:
            logger.info(
                "RAG 只读挂载就绪：%s 块（未写库；灌库请运行 scripts/ingest_knowledge.py）",
                stats.chunks,
            )
        pending = rag.pending_changes()
        if pending:
            # 不是致命错，但必须喊出来：改了知识库却忘了跑灌库 Job，
            # 用户会拿到旧答案，而 /health 一直是 ok。
            logger.warning(
                "知识库有 %d 个文件的改动尚未灌库，检索结果可能过期：%s",
                len(pending),
                ", ".join(pending[:5]) + ("..." if len(pending) > 5 else ""),
            )
        if settings.rag_require_index and (stats.degraded or stats.chunks <= 0):
            raise ProductionConfigError(
                "rag_ingest_on_startup=false 且 rag_require_index=true，但索引不可用："
                f"{stats.error or '集合内没有任何块'}。请先运行 "
                "python scripts/ingest_knowledge.py --force 再启动。"
            )

    # ---- Phase 7：知识条目合规元数据（来源/审核人/复核日期/失效日期）----
    # 只读挂载也必须查：索引可以用，但内容可能已过期或不可溯源。
    # 开发态只告警（存量知识库还没补齐 frontmatter 时也要能跑），
    # production 门禁强制 rag_require_doc_meta=true → 直接拒绝启动。
    meta_issues = collect_meta_issues(Path(settings.knowledge_dir))
    if meta_issues:
        detail = "；".join(
            f"{item['file']}（{'、'.join(item['problems'])}）" for item in meta_issues[:5]
        )
        if settings.rag_require_doc_meta:
            raise ProductionConfigError(
                f"知识库有 {len(meta_issues)} 个文件元数据不合规（rag_require_doc_meta=true）："
                f"{detail}。请补齐 frontmatter 的 source/reviewer/reviewed_at/expires_at。"
            )
        logger.warning(
            "知识库有 %d 个文件元数据不合规（来源/审核人/失效日期缺失或已过期）：%s",
            len(meta_issues),
            detail,
        )
    # ---- 院内系统 ----
    doctor_service = VectorStoreDoctorService(
        retrieve_all_chunks=vector_store.all_texts,
        roster_file=settings.rag_excluded_files[0] if settings.rag_excluded_files else "doctors.md",
    )
    # 本地 HIS 替身（slot_service_impl=local_his 时才启动）：
    # 内嵌后台线程 + ThreadingHTTPServer，零外部进程；状态落本地 JSON。
    local_his_server = None
    if settings.slot_service_impl.strip().lower() == "local_his":
        local_his_server = LocalHisServer(
            persist_path=(
                Path(settings.local_his_state_path)
                if settings.local_his_state_path.strip()
                else None
            )
        )
    slot_service = _build_slot_service(
        settings, doctor_service, local_his_server=local_his_server
    )

    # ---- 仓储 ----
    # Phase 8-B（P0-06）：开启后身份证号密文落库 + HMAC 指纹查重。
    # 关闭（默认）时 pii_codec=None → SQL 与一期逐字一致。
    pii_codec = build_pii_codec(
        enabled=settings.pii_protection_enabled,
        secret_key=settings.pii_secret_key,
    )
    if pii_codec is not None:
        logger.info("预约敏感字段保护：已开启（密文落库 + HMAC 指纹查重）")
    repository = MySQLAppointmentRepository(
        settings.mysql_host,
        settings.mysql_port,
        settings.mysql_user,
        settings.mysql_password,
        settings.mysql_database,
        pii_codec=pii_codec,
    )

    # ---- 记忆 ----
    # Phase 5（P1-01）：storage_fail_fast=true 时 Mongo strict + 不包降级 ——
    # 存储故障表现为 503 与 /ready 失败，绝不静默切本地文件
    # （本地文件不是多副本共享会话的合法故障转移目标，降级期间会分裂数据源）。
    if settings.storage_fail_fast:
        conversation_store = MongoConversationStore(
            settings.mongo_uri,
            settings.mongo_database,
            strict=True,
        )
        logger.info("会话存储：strict 快速失败模式（storage_fail_fast=true）")
    else:
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
            BookAppointmentTool(
                repository,
                slot_service,
                doctor_service,
                patient_ref_salt=settings.his_patient_ref_salt,
            ),
            QuerySlotTool(slot_service, doctor_service),
            QueryDoctorsTool(doctor_service),
            # 取消也要号源服务（真实 HIS 退号必须通知医院）
            CancelAppointmentTool(repository, slot_service),
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

    async def _probe_mongo() -> dict:
        # strict 模式下 Mongo 是硬依赖：挂了 /ready 必须失败（P1-01）
        await conversation_store.ping()
        return {"ok": True}

    health.register("model", _probe_model)
    health.register("milvus", _probe_milvus)
    health.register("mysql", _probe_mysql)
    if settings.storage_fail_fast:
        health.register("mongo", _probe_mongo)
    if settings.rate_limit_backend.strip().lower() == "redis":
        # 限流后端是硬依赖（fail-closed 下故障就 503），必须进就绪探针，
        # 否则 /ready 报就绪但聊天全被拒 = 假健康。
        def _probe_redis() -> dict:
            rate_limiter.probe()
            return {"ok": True}

        health.register("redis", _probe_redis)

    # ---- 权限 ----
    permission_engine = PermissionDecisionEngine.from_yaml(Path(settings.permissions_file))
    # 未在 YAML 显式配置的工具按风险等级推导默认策略（显式配置优先）——
    # 新增只读工具不再需要手动改 permissions.yaml
    for definition in tool_registry.list_definitions():
        permission_engine.register_tool_default(definition.name, definition.risk_level)

    # ---- 审批闭环（Phase 2，灰度默认关）----
    # 开启时先做**注册期契约体检**，再确保 MySQL 表就绪；任何失败直接抛错，
    # 绝不「禁用 + 告警」静默降级成无审批放行（画像 alias 坑的教训）。
    approval_service: Optional[ApprovalService] = None
    if settings.approval_enabled:
        problems = tool_registry.validate_contracts()
        if problems:
            raise ProductionConfigError(
                "审批已开启但工具契约不合规，拒绝启动：\n  - " + "\n  - ".join(problems)
            )
        approval_repo = MySQLApprovalRepository(
            settings.mysql_host,
            settings.mysql_port,
            settings.mysql_user,
            settings.mysql_password,
            settings.mysql_database,
        )
        approval_repo.ensure_table()
        approval_service = ApprovalService(
            approval_repo, ttl_seconds=settings.approval_ttl_seconds
        )
        if not settings.auth_enabled:
            logger.warning(
                "审批已开启但认证关闭：所有主体退化为 anonymous，"
                "任何访客都能确认他人的写操作；仅限本机单机调试"
            )
        logger.info(
            "审批闭环已启用：approval_requests 就绪，TTL=%ss，写工具需一次性确认",
            settings.approval_ttl_seconds,
        )

    secure_executor = SecureToolExecutor(
        permission_engine,
        no_ui_policy=settings.permission_no_ui,
        audit_logger=audit,
        approval_service=approval_service,
        metrics=metrics,
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
        task_tracker=task_tracker,
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
        local_his_server=local_his_server,
        shutdown=shutdown,
        profile_service=profile_service,
        token_service=token_service,
        session_access=session_access,
        approval_service=approval_service,
    )
