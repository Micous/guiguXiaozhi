"""配置管理 —— 对齐参考项目 mini_harness/utils/config.py 的单例模式。

配置优先级：环境变量 > .env 文件 > 默认值。
所有密钥与连接信息都从这里出，不散落在代码里。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """全局配置"""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 运行环境 ----------
    # development（默认）：本地开发 / 内网演示，沿用宽松默认值；
    # production：启动时执行 config_guard 的 fail-fast 校验，不合规直接拒绝启动。
    # 注意：production 是一个**门禁**而非"更快的配置"，当前仓库没有真实 HIS 适配器，
    # 因此该模式下必然启动失败（这是刻意的，见 config_guard.py）。
    app_env: str = "development"
    # 号源适配器选择（组合根据此装配）：mock = 本地模拟排班；
    # 真实 HIS 适配器在 Phase 3b 落地后于 application._build_slot_service 注册。
    slot_service_impl: str = "mock"

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8081  # 与 Java 版（8080）并存，便于 A/B 对比

    # ---------- 模型（Ollama） ----------
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5:7b"
    ollama_embedding_model: str = "andersc/qwen3-embedding:0.6b"
    ollama_temperature: float = 0.1  # 小模型工具调用稳定性
    ollama_num_ctx: int = 8192
    ollama_timeout: float = 300.0
    ollama_max_retries: int = 2  # 空响应重试次数

    # ---------- 向量库（Milvus） ----------
    milvus_uri: str = "http://localhost:19530"
    milvus_alias: str = "xiaozhi_knowledge_py"  # 与 Java 版分开，便于 A/B 对比
    milvus_metric_type: str = "COSINE"
    milvus_index_type: str = "FLAT"
    knowledge_dir: str = str(ROOT / "xiaozhi" / "knowledge")
    manifest_path: str = ""  # 留空则按集合名自动生成（不同集合不共用清单）

    # ---------- 检索 ----------
    rag_max_results: int = 2
    rag_dense_top_k: int = 20
    rag_sparse_top_k: int = 20
    # 阈值必须跟分块策略一起标定（见 踩坑与优化记录.md 3.11）
    rag_dense_min_score: float = 0.35
    # 专家名单不参与聊天 RAG，只通过"查询科室专家"工具暴露
    rag_excluded_files: List[str] = Field(default_factory=lambda: ["doctors.md"])

    # ---------- 数据库 ----------
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "123456"
    mysql_database: str = "guiguxiaozhi"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "chat_memory_db"
    # Phase 5（P1-01，灰度默认关）：会话存储快速失败模式。
    # true：Mongo strict（故障抛 RepositoryUnavailable 不伪装空历史）+ 不包
    #   Resilient 本地文件降级 + chat 前置 ping 探测失败返回 503；
    # false：一期行为（Resilient 降级），内网演示/开发不受影响。
    # production 门禁强制要求 true（config_guard 第 7 项）。
    storage_fail_fast: bool = False

    # ---------- Phase 5（P1-02）：任务生命周期持久化（灰度默认关） ----------
    # true：AgentTask/AgentExecution/ToolExecution/TaskEvent 落 MySQL，
    #   启动时执行崩溃恢复扫描（超时 RUNNING→INTERRUPTED、STARTED→UNKNOWN）；
    # false：一期行为（请求内局部状态），引擎零额外开销。
    task_tracking_enabled: bool = False
    # 恢复扫描：运行超过该秒数且无更新的 execution 视为进程已死
    task_stale_after_seconds: float = 600.0

    # ---------- 记忆 ----------
    memory_max_messages: int = 20
    memory_token_budget: int = 4000

    # ---------- 运行时 ----------
    max_turns: int = 6
    max_tool_calls: int = 8  # 单请求内最大工具调用次数，防死循环
    request_timeout: float = 300.0

    # ---------- 工具权限 ----------
    permissions_file: str = str(ROOT / "config" / "permissions.yaml")
    # 无用户交互通道时，ASK 级工具的处理方式：
    #   allow —— 放行（用户确认已由系统提示词的对话流程承担，权限层只做越权防护）
    #   deny  —— 拒绝（更严格，适合生产环境按需开启）
    permission_no_ui: str = "allow"
    # Phase 2 审批闭环（灰度默认关）：开启后写工具必须先经用户明确确认，
    # 凭一次性 token 才能执行；关闭时 SecureToolExecutor 行为与一期完全一致。
    approval_enabled: bool = False
    approval_ttl_seconds: float = 120.0

    # ---------- 可靠性 ----------
    retry_backoff_base: float = 0.1
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout: float = 30.0
    tool_failure_threshold: int = 3  # 单请求内某工具连续失败 N 次即熔断该工具
    drain_timeout: float = 10.0

    # ---------- 限流 ----------
    enable_rate_limit: bool = True
    rate_limit_max_requests: int = 30
    rate_limit_window_seconds: float = 60.0

    # ---------- 患者画像（P2 长期记忆，灰度默认关） ----------
    profile_enabled: bool = False
    # 每积累 N 轮新消息抽取一次画像
    profile_extract_interval_turns: int = 6
    # Fernet 密钥；开启画像时必填，缺失则画像模块整体禁用（敏感字段必须密文落库）
    profile_secret_key: str = ""

    # ---------- 认证（Phase 1，灰度默认关）----------
    # auth_enabled=false：完全沿用一期行为（memoryId/userId 由请求体自报，前端零改动）；
    # auth_enabled=true ：/xiaozhi/chat 必须带 Bearer token，memoryId 必须是服务端
    #   签发的 session_id 且归属该主体；运维端点需 ops:read scope。
    # 正式环境换 OIDC/网关时只换签发方，TokenService.verify 契约不变。
    auth_enabled: bool = False
    auth_secret_key: str = ""  # JWT HS256 签名密钥（开启时必填，生产要求 ≥32 字符）
    auth_bootstrap_secret: str = ""  # 本地过渡：/api/v1/auth/token 的换取凭证
    auth_token_ttl_seconds: int = 3600
    auth_issuer: str = "xiaozhi-local"
    auth_audience: str = "xiaozhi-chat"  # JWT 受众，防止别处签发的 token 被本服务接受
    auth_ops_subjects: List[str] = Field(default_factory=list)  # 可访问运维端点的主体

    # ---------- 可观测 ----------
    log_level: str = "INFO"
    json_logs: bool = True
    enable_metrics: bool = True
    trace_max: int = 500
    trace_persist_dir: str = str(ROOT / "logs" / "traces")
    audit_log_path: str = str(ROOT / "logs" / "audit.jsonl")

    @property
    def is_production(self) -> bool:
        """是否以生产形态启动（大小写与 prod 别名不敏感）"""
        return self.app_env.strip().lower() in ("production", "prod")

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置（单例）"""
    return Settings()


def reset_settings() -> None:
    """重置配置（测试用）"""
    get_settings.cache_clear()
