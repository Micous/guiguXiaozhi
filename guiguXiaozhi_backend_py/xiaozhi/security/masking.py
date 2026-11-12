"""敏感信息掩码 —— 医疗场景的唯一「展示口径」（P0-06 的入口侧收口）。

**为什么要有独立模块**

改造前同一件事被实现了三遍，而且口径不同：

* `security/approval_service.py::mask_sensitive_args` —— 审批表/前端展示用
  （前 6 位 + 星号 + 后 4 位）；
* `reliability/redaction.py` —— 日志/trace 用（整体替换成 ``[REDACTED]``）；
* 工具层 —— **完全没有**：``预约成功。姓名：张三，身份证号：610121199401032831``
  直接回给模型 → 模型转述给用户 → 同一段文本又进历史落库。

审计 P0-06 的原话是「工具返回文本里回显完整身份证号」。掩码口径必须只有
一处定义，否则"改了日志忘了工具"这种事一定会再发生。

本模块只做**纯函数、无 IO、无配置**的掩码变换，任何层都可以安全调用：

* :func:`mask_id_card` —— 单个身份证号；
* :func:`mask_phone` —— 单个手机号；
* :func:`mask_pii_in_text` —— 自由文本里的全部身份证/手机号（模型回复、
  校验错误、工具结果都可能夹带）；
* :func:`mask_sensitive_value` —— 按**键名**判断的通用脱敏（审批参数用）。

**刻意不做的事**

* 不 mask 姓名：姓名的可识别性远低于身份证号，全打码会让"张*"和"李*"
  无法区分，用户核对预约信息时反而容易约错人；
* 不做可逆加密 —— 那不是掩码的职责，见 ``security/pii.py``。
"""

from __future__ import annotations

import re
from typing import Any, Dict

# 18 位身份证（末位可为 X）与 15 位旧号。
# 中文与数字之间不存在 \b 词边界（中文属 \w），必须用前后瞻断言。
_ID_CARD_RE = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)")
# 中国大陆手机号
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

#: 键名命中即按敏感值处理（与 approval_service 的历史口径一致）
SENSITIVE_KEY_PARTS = (
    "id_card",
    "idcard",
    "identity",
    "身份证",
    "phone",
    "mobile",
    "手机号",
)

MASK_CHAR = "*"


def mask_id_card(value: str) -> str:
    """身份证号掩码：保留前 6 位（地区码）与后 4 位（含校验位）。

    >>> mask_id_card("610121199401032831")
    '610121********2831'
    >>> mask_id_card("123")
    '***'
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) >= 11:
        return f"{text[:6]}{MASK_CHAR * (len(text) - 10)}{text[-4:]}"
    return MASK_CHAR * len(text)


def mask_phone(value: str) -> str:
    """手机号掩码：保留前 3 位与后 4 位。"""
    text = (value or "").strip()
    if len(text) >= 8:
        return f"{text[:3]}{MASK_CHAR * (len(text) - 7)}{text[-4:]}"
    if not text:
        return ""
    return MASK_CHAR * len(text)


def mask_pii_in_text(text: str) -> str:
    """把自由文本里的身份证号、手机号替换成掩码。

    **幂等**：对已掩码的文本再跑一次不会破坏掩码
    （掩码后的串不再匹配 ``\\d{17}`` —— 中间是星号）。
    """
    if not text:
        return text
    masked = _ID_CARD_RE.sub(lambda m: mask_id_card(m.group(0)), text)
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), masked)


def mask_sensitive_value(key: str, value: Any) -> Any:
    """按键名判断的通用脱敏（审批参数 / 展示副本用）。

    非敏感键原样返回；非字符串值不做处理（长度未知时盲目打码会改类型）。
    """
    if not isinstance(value, str):
        return value
    lowered = str(key).lower()
    if not any(token in lowered for token in SENSITIVE_KEY_PARTS):
        return value
    return mask_id_card(value) if value else ""


def mask_sensitive_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """生成**仅用于展示/落表/落历史**的脱敏副本（不原地修改入参）。"""
    preview: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            preview[key] = mask_sensitive_value(str(key), value)
        else:
            preview[key] = mask_pii_in_text(str(value))
    return preview
