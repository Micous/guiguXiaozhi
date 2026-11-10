"""通用护栏：输入体检与输出体检。

对应 PLAN.md「P3 guardrails」。刻意做成**不轻易拒绝**：
聊天场景里"忽略上面的话"这类表述可能是患者在描述自己的症状，
误拦比漏拦更伤体验 —— 所以注入只留痕、由 L3 工具层决定是否拒绝；
这里真正做的是长度约束、敏感值脱敏、以及"没调工具却声称预约成功"的标记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from xiaozhi.models.quality import detect_false_confirmation
from xiaozhi.reliability.logging import redact_text
from xiaozhi.security.injection import detect_injection

MAX_INPUT_CHARS = 2000


@dataclass
class GuardrailResult:
    allowed: bool = True
    text: str = ""
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reasons": list(self.reasons)}


def check_input(text: str, *, max_chars: int = MAX_INPUT_CHARS) -> GuardrailResult:
    """输入体检：超长截断、注入检测只留痕不拦截"""
    result = GuardrailResult(text=text or "")
    if len(result.text) > max_chars:
        result.text = result.text[:max_chars]
        result.reasons.append(f"输入超过 {max_chars} 字，已截断")

    finding = detect_injection(result.text)
    if finding.detected:
        # 不阻断：交给工具层的 L3 注入防护按上下文判断
        result.reasons.append(f"疑似越权指令（仅记录）：{finding.reason}")
    return result


def check_output(text: str, *, tool_executed: bool = False) -> GuardrailResult:
    """输出体检：敏感信息打码 + 幻觉承诺标记"""
    result = GuardrailResult(text=redact_text(text or ""))
    if detect_false_confirmation(
        result.text, has_tool_calls=False, tool_executed=tool_executed
    ):
        result.reasons.append("未调用工具却声称预约/取消成功")
    return result
