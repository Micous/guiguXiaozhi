"""预约数据的敏感字段保护 —— 审计 P0-06 的落库侧（Phase 8-B）。

**问题是什么**

`appointment` 表把身份证号明文存了两年：

* 一次库泄露 = 一批患者身份证号（含姓名 + 就诊科室 + 就诊时间，
  足以还原"某人某天在协和看什么病"）；
* 唯一约束 `uk_appointment_active` 直接建在明文列上 —— 想加密都加不了：
  Fernet 是**随机化**加密，同一身份证号每次密文不同，唯一约束立刻失效。

**方案：三段分离，各司其职**

============  ==============================================================
列             内容
============  ==============================================================
``id_card``    ``enc:v1:<fernet token>`` —— 可逆密文（HIS 对账、运维需要原值）
``id_card_hash``  HMAC-SHA256(key, 身份证号) —— **确定性**，唯一约束与查重键
``id_card_masked`` 掩码（``610121********2831``）—— 列表页/审计展示直接读
============  ==============================================================

为什么查重键用 **HMAC 而不是裸 SHA-256**：身份证号空间是可枚举的
（地区码 + 生日 + 顺序码，实际有效组合在 10^10~10^12 量级），
裸哈希可以离线爆破出原值；带密钥的 HMAC 在没有密钥时无法枚举。
（``appointment_repo.Appointment.make_patient_ref`` 已在用带盐哈希，
口径一致。）

**灰度**：``PII_PROTECTION_ENABLED=false``（默认）时仓储完全不碰本模块，
SQL 与一期逐字一致；开启前必须先跑 ``scripts/pii_protect_appointments.py``
把存量行回填，否则老数据查不到（``hash`` 为空）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Optional

from xiaozhi.security.masking import mask_id_card

logger = logging.getLogger(__name__)

#: 密文前缀 —— 用于区分"已是密文"和"存量明文/未回填"
CIPHER_PREFIX = "enc:v1:"

#: 查重/唯一约束用的摘要前缀（与明文列区分，避免同列混用）
HASH_VERSION = "hmac-sha256"

_HASH_KEY_LABEL = b"xiaozhi|appointment|id_card_hash|v1"


class PiiConfigError(RuntimeError):
    """密钥缺失/格式错误 —— 开启保护却不给密钥，必须拒绝而不是静默降级。"""


class PiiCodec:
    """身份证号的加密 / 指纹 / 掩码三件套。

    构造即校验密钥：Fernet 要求 32 字节 urlsafe-base64。
    """

    def __init__(self, secret_key: str) -> None:
        if not (secret_key or "").strip():
            raise PiiConfigError(
                "pii_secret_key 为空：开启 PII 保护必须提供 Fernet 密钥"
                "（python -c \"from cryptography.fernet import Fernet;"
                ' print(Fernet.generate_key().decode())"）'
            )
        from cryptography.fernet import Fernet

        try:
            self._fernet = Fernet(secret_key.strip().encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 密钥格式问题的统一出口
            raise PiiConfigError(f"pii_secret_key 不是合法的 Fernet 密钥：{exc}") from exc
        # HMAC key 从同一个 Fernet 密钥派生：一份配置管两个用途，
        # 减少"配了加密没配指纹密钥"的半残状态。
        raw = base64.urlsafe_b64decode(secret_key.strip().encode("utf-8"))
        self._hash_key = hmac.new(raw, _HASH_KEY_LABEL, hashlib.sha256).digest()

    # ---- 指纹（确定性，可做唯一约束） ----

    def fingerprint(self, value: str) -> str:
        normalized = (value or "").strip().upper()
        if not normalized:
            return ""
        return hmac.new(
            self._hash_key, normalized.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # ---- 密文（可逆，供 HIS 对账与运维核对） ----

    def encrypt(self, value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        return CIPHER_PREFIX + self._fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def decrypt(self, stored: str) -> str:
        """解密；非密文（存量明文/空值）原样返回 —— 便于迁移期混存。"""
        text = (stored or "").strip()
        if not text.startswith(CIPHER_PREFIX):
            return text
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(
                text[len(CIPHER_PREFIX) :].encode("utf-8")
            ).decode("utf-8")
        except InvalidToken as exc:
            # 换过密钥的库：不能静默返回空串（会变成"查不到人"），
            # 也不能抛到业务层（一次坏记录不该让整张列表 500）
            logger.error("预约记录中的身份证密文无法解密（密钥不匹配？）")
            raise PiiConfigError("身份证密文无法解密：请确认 pii_secret_key 与写入时一致") from exc

    @staticmethod
    def masked(value: str) -> str:
        return mask_id_card(value)

    def protect(self, value: str) -> tuple[str, str, str]:
        """(密文, 指纹, 掩码) —— 写入时的三个落库值。"""
        raw = (value or "").strip()
        return self.encrypt(raw), self.fingerprint(raw), self.masked(raw)


def is_ciphertext(stored: str) -> bool:
    return (stored or "").strip().startswith(CIPHER_PREFIX)


def build_pii_codec(
    *,
    enabled: bool,
    secret_key: str,
) -> Optional[PiiCodec]:
    """按灰度开关构造编解码器（关闭返回 None = 一期行为）。

    开启但密钥缺失 → 抛 ``PiiConfigError``，**不静默降级**：
    静默降级的后果是"以为加密了，其实还是明文"，比不加密更危险。
    """
    if not enabled:
        return None
    return PiiCodec(secret_key)
