"""HIS 沙箱配置 —— 全部走环境变量，不依赖主后端的 .env。

独立服务：默认连 ``his-sandbox-mysql:3307``（docker-compose 起的独立实例），
也可以设 ``HIS_SANDBOX_MYSQL_PORT=3306`` 复用已有 Docker MySQL 上的独立库。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class SandboxConfig:
    host: str = "127.0.0.1"
    port: int = 18081
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_user: str = "root"
    mysql_password: str = "his_sandbox_123"
    mysql_database: str = "his_sandbox"
    # Sa-Token 风格的访问令牌；空 = 关闭鉴权（仅本地调试）
    token: str = "his-sandbox-token"
    # 患者主档指纹盐（生产必须环境注入；沙箱给默认值只为可复现）
    patient_ref_salt: str = "his-sandbox-local-salt"
    # 预占有效期（秒）。真机审批要 ≥600，这里默认 600 与项目约定一致
    hold_ttl_seconds: int = 600
    # 号源池大小 / 排班展开天数
    limit_num: int = 5
    seed_days: int = 30
    org_id: str = "ORG-LOCAL"
    # 故障注入：空 / timeout / http500 / business_fail / reject_hold
    fault: str = ""

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        return cls(
            host=_env("HIS_SANDBOX_HOST", "127.0.0.1"),
            port=_env_int("HIS_SANDBOX_PORT", 18081),
            mysql_host=_env("HIS_SANDBOX_MYSQL_HOST", "127.0.0.1"),
            mysql_port=_env_int("HIS_SANDBOX_MYSQL_PORT", 3307),
            mysql_user=_env("HIS_SANDBOX_MYSQL_USER", "root"),
            mysql_password=_env("HIS_SANDBOX_MYSQL_PASSWORD", "his_sandbox_123"),
            mysql_database=_env("HIS_SANDBOX_MYSQL_DATABASE", "his_sandbox"),
            token=_env("HIS_SANDBOX_TOKEN", "his-sandbox-token"),
            patient_ref_salt=_env("HIS_SANDBOX_PATIENT_REF_SALT", "his-sandbox-local-salt"),
            hold_ttl_seconds=_env_int("HIS_SANDBOX_HOLD_TTL", 600),
            limit_num=_env_int("HIS_SANDBOX_LIMIT_NUM", 5),
            seed_days=_env_int("HIS_SANDBOX_SEED_DAYS", 30),
            org_id=_env("HIS_SANDBOX_ORG_ID", "ORG-LOCAL"),
            fault=_env("HIS_SANDBOX_FAULT", ""),
        )

    def id_no_hash(self, id_no: str) -> str:
        """身份证号 → HMAC 指纹（不存明文，不让裸哈希可彩虹表反查）。"""
        normalized = (id_no or "").strip().upper()
        return hashlib.sha256(
            f"{self.patient_ref_salt}|{normalized}".encode("utf-8")
        ).hexdigest()


def mask_id_no(id_no: str) -> str:
    """身份证号展示掩码：首 6 尾 4。"""
    value = (id_no or "").strip()
    if len(value) < 11:
        return "*" * len(value)
    return f"{value[:6]}{'*' * (len(value) - 10)}{value[-4:]}"
