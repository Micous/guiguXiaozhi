"""可观测数据脱敏 —— 对齐参考项目 mini_harness/reliability/redaction.py。

硅谷小智是医疗场景，身份证号属于敏感个人信息，必须在日志与 trace 中打码。
"""

from __future__ import annotations

import re
from typing import Any, Tuple

# 键名命中即整体脱敏
SENSITIVE_KEY_PARTS: Tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "cookie",
    "access_token",
    "auth_token",
    "bearer",
    "id_card",
    "idcard",
    "身份证",
    "phone",
    "mobile",
    "email",
)

REDACTED = "[REDACTED]"
MAX_TEXT_LENGTH = 500

# 值形态命中即脱敏（与键名无关）
SECRET_PATTERNS = (
    # 18 位身份证号（含校验位）
    re.compile(r"\b[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"),
    re.compile(r"\bsk-[A-Za-z0-9._-]+"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.I),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),  # 手机号
)


# 这些键名虽然含 token，但只是**计数**，不是凭证，不该打码
COUNT_KEYS = ("prompt_tokens", "eval_tokens", "total_tokens", "token_count", "tokens")


def sanitize_observability_value(key: str, value: Any) -> Any:
    """对可观测数据做脱敏 + 长度截断"""
    key_lower = str(key).lower()
    if key_lower in COUNT_KEYS:
        return value
    if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
        return REDACTED

    if isinstance(value, dict):
        return {
            str(k): sanitize_observability_value(str(k), v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_observability_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_observability_value(key, item) for item in value)
    if isinstance(value, str):
        sanitized = value
        for pattern in SECRET_PATTERNS:
            sanitized = pattern.sub(REDACTED, sanitized)
        if len(sanitized) > MAX_TEXT_LENGTH:
            sanitized = sanitized[:MAX_TEXT_LENGTH] + "...[truncated]"
        return sanitized

    return value


def contains_sensitive_value(value: str) -> bool:
    """判断字符串中是否含敏感信息"""
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)
