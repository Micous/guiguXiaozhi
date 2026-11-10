"""提示词注入检测。

模型与用户的输入都会进入提示词，恶意用户可能尝试"忽略之前的指令"。
这里做**标记与拦截**，但不做过度拦截 —— 误杀"取消预约"这类正常指令的代价更大。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# 明确的越权指令模式
INJECTION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"忽略(以上|上面|之前|前面)(的)?(所有)?(指令|规则|提示)", "试图让模型忽略系统指令"),
    (r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|rules|prompts)", "试图让模型忽略系统指令"),
    (r"(你|现在)(是|扮演|作为)\s*(一个)?\s*(没有限制|无限制|不受限|开发者模式)", "试图让模型切换到无限制模式"),
    (r"(reveal|print|show)\s+(your\s+)?(system\s+)?prompt", "试图套取系统提示词"),
    (r"(输出|打印|告诉我)(你的)?(系统)?(提示词|prompt|规则)", "试图套取系统提示词"),
    (r"(直接|立即)?(执行|运行)\s*(任意|所有)?(命令|代码|脚本)", "试图让模型执行任意命令"),
    (r"drop\s+(table|database)\s", "疑似破坏性数据库操作"),
    (r"truncate\s+table\s", "疑似破坏性数据库操作"),
)


@dataclass
class InjectionFinding:
    """一次注入检测结果"""

    detected: bool
    patterns: List[str] = None  # type: ignore[assignment]
    reason: str = ""

    def __post_init__(self) -> None:
        if self.patterns is None:
            self.patterns = []


def detect_injection(text: str) -> InjectionFinding:
    """检测文本中的注入意图"""
    if not text:
        return InjectionFinding(detected=False)
    lowered = text.lower()
    matched: List[str] = []
    for pattern, reason in INJECTION_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            matched.append(reason)
    if matched:
        return InjectionFinding(detected=True, patterns=matched, reason=matched[0])
    return InjectionFinding(detected=False)
