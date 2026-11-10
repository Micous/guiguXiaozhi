"""模型输出质量门控。

对应 PLAN.md「P1 质量门控」与「四、必须移植的踩坑修复」第 3、8 条：

* Ollama 在提示词缓存命中时会返回 HTTP 200 但内容为空 → 需要判定"空响应"以便重试；
* 提示词里若写了"不要提及工具"，小模型会把工具调用**写成纯文本 JSON**吐出来 → 需要识别；
* 小模型偶尔不调工具却说"已为您预约成功" → 幻觉承诺，必须留痕并给用户提示。

这里全部是**纯函数**，不依赖任何外部服务，方便单测。真正的重试与降级在
``runtime/engine.py`` 与 ``reliability/resilience.py`` 里执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 模型没调工具却声称完成了写操作（Java 版实测：temperature 高时会出现）
_FALSE_CONFIRMATION = re.compile(
    r"已(经)?(为您|替您|帮你|替你)?(成功)?(预约|挂号)"
    r"|预约(已经)?(成功|完成|提交成功)"
    r"|挂号(成功|已完成)"
    r"|已(成功)?取消(预约|挂号)"
    r"|预约号[:：]\s*[A-Za-z0-9-]+"
)

# 工具调用被当成纯文本输出（形如「预约挂号\n{...}」）
_TEXTUAL_TOOL_CALL = re.compile(
    r"(预约挂号|取消预约|查询是否有号源|查询科室专家)\s*[{\n\r]"
)

# 兜底提示：不下结论说预约失败，只提醒以系统记录为准
FALSE_CONFIRMATION_NOTICE = (
    "\n\n（提示：本次未检测到系统预约记录，请以查询结果为准，不要视为预约已成功。）"
)


@dataclass
class QualityFinding:
    """一次模型输出的体检结果"""

    empty: bool = False
    invalid_tool_calls: List[str] = field(default_factory=list)
    false_confirmation: bool = False
    textual_tool_call: bool = False
    reasons: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def needs_retry(self) -> bool:
        """空响应 / 把工具调用写成文本，都值得重来一次"""
        return self.empty or self.textual_tool_call


def is_empty_turn(text: str, tool_calls: Sequence[Any]) -> bool:
    """HTTP 200 但既没有文本、也没有工具调用 —— Ollama 的已知行为"""
    return not (text or "").strip() and not tool_calls


def inspect_tool_calls(
    tool_calls: Sequence[Any],
    known_tool_names: Optional[Sequence[str]] = None,
) -> List[str]:
    """校验工具调用的基本合法性，返回问题描述列表"""
    problems: List[str] = []
    known = set(known_tool_names or [])
    for call in tool_calls:
        name = str(getattr(call, "name", "") or "").strip()
        arguments = getattr(call, "arguments", None)
        if not name:
            problems.append("工具调用缺少名称")
            continue
        if known and name not in known:
            problems.append(f"工具【{name}】未注册")
        if arguments is not None and not isinstance(arguments, dict):
            problems.append(f"工具【{name}】的参数不是对象：{type(arguments).__name__}")
    return problems


def detect_false_confirmation(
    text: str,
    *,
    has_tool_calls: bool = False,
    tool_executed: bool = False,
) -> bool:
    """是否在没有任何工具调用的情况下声称已预约或已取消成功"""
    if has_tool_calls or tool_executed:
        return False
    return bool(_FALSE_CONFIRMATION.search(text or ""))


def detect_textual_tool_call(text: str) -> bool:
    """工具调用被改写成纯文本输出"""
    return bool(_TEXTUAL_TOOL_CALL.search(text or ""))


def inspect_turn(
    turn: Any,
    known_tool_names: Optional[Sequence[str]] = None,
    *,
    tool_executed: bool = False,
) -> QualityFinding:
    """体检一次模型输出。

    ``turn`` 只要具备 ``text`` 与 ``tool_calls`` 两个属性即可（兼容 ChatTurn 与测试替身）。
    """
    text = str(getattr(turn, "text", "") or "")
    tool_calls = list(getattr(turn, "tool_calls", None) or [])

    finding = QualityFinding()
    finding.empty = is_empty_turn(text, tool_calls)
    finding.invalid_tool_calls = inspect_tool_calls(tool_calls, known_tool_names)
    finding.false_confirmation = detect_false_confirmation(
        text, has_tool_calls=bool(tool_calls), tool_executed=tool_executed
    )
    finding.textual_tool_call = detect_textual_tool_call(text)

    if finding.empty:
        finding.reasons.append("空响应（无文本、无工具调用）")
    if finding.textual_tool_call:
        finding.reasons.append("疑似把工具调用写成了纯文本")
    finding.reasons.extend(finding.invalid_tool_calls)
    if finding.false_confirmation:
        finding.reasons.append("未调用工具却声称预约/取消成功")
    return finding


def strip_textual_tool_call(text: str) -> str:
    """把纯文本形式的工具调用从回答里裁掉（用于重试前的清理）"""
    match = _TEXTUAL_TOOL_CALL.search(text or "")
    if not match:
        return text
    return (text[: match.start()] or "").rstrip()
