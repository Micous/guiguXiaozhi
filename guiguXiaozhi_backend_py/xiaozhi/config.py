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

    # ---------- 可观测 ----------
    log_level: str = "INFO"
    json_logs: bool = True
    enable_metrics: bool = True
    trace_max: int = 500
    trace_persist_dir: str = str(ROOT / "logs" / "traces")
    audit_log_path: str = str(ROOT / "logs" / "audit.jsonl")

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
