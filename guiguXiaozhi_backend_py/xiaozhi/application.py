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
from xiaozhi.core.tool import Tool
from xiaozhi.memory.context import ContextAssembler
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
from xiaozhi.reliability.logging import configure_logging
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig
from xiaozhi.reliability.trace_store import TraceStore, set_default_store
from xiaozhi.repositories.appointment_repo import (
    AppointmentRepository,
    InMemoryAppointmentRepository,
    MySQLAppointmentRepository,
)
from xiaozhi.retrieval.rag import RagService
from xiaozhi.retrieval.vector_store import MilvusVectorStore
from xiaozhi.runtime.engine import AgentEngine
from xiaozhi.security.permissions import PermissionDecisionEngine
from xiaozhi.security.secure_executor import SecureToolExecutor
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

    async def aclose(self) -> None:
        """优雅停机：释放连接"""
        await self.chat_model.aclose()
        await self.embedding_model.aclose()
        await self.conversation_store.close()
        self.audit.close()


def load_system_prompt() -> str:
    """读取系统提示词（与 Java 版同一份内容），并替换模板变量。

    Java 版用的是 LangChain4j 的 PromptTemplate，``{{current_date}}`` 会自动替换；
    这里自己替换，否则模型会看到字面量 ``{{current_date}}``。
    """
    from datetime import date

    path = PROMPT_DIR / "xiaozhi-prompt.txt"
    if not path.exists():
        return "你是硅谷小智，北京协和医院的智能客服与医疗顾问。"
    text = path.read_text(encoding="utf-8")
    return text.replace("{{current_date}}", date.today().isoformat())


async def get_application(settings: Optional[Settings] = None) -> Application:
    """装配整个应用"""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.json_logs)

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
    slot_service = MockSlotService(doctor_service)  # ← 唯一 Mock：外部 HIS 接口本地调不通

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

    # ---- 运行时 ----
    engine = AgentEngine(
        chat_model=chat_model,
        tool_registry=tool_registry,
        secure_executor=secure_executor,
        conversation_store=conversation_store,
        context_assembler=ContextAssembler(token_budget=settings.memory_token_budget),
        system_prompt=load_system_prompt(),
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
    )
