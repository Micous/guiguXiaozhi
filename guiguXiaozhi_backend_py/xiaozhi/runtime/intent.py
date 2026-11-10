"""意图识别 —— 决定要不要注入 RAG 知识。

**为什么需要这一层（实测结论）**：

在用户消息里注入"请参考以下医院知识库内容回答用户问题"之后，
qwen2.5:7b 的**工具调用率从 5/5 掉到 0/5** —— 它看到"请参考知识回答"，
就当成"这是道知识题"，于是直接答题，不再调用工具。
（把知识放进 system message 是 1/5，用标签包裹是 3/5，都不行。）

但挂号/取消/查号源/查专家这类**操作请求**，本来就该由工具给出权威答案，
不需要知识库。所以：**操作类请求不注入知识**，其余照常注入。
"""

from __future__ import annotations

import re
from typing import Tuple

# 操作类意图关键词（命中即跳过 RAG 注入）
_OPERATION_PATTERNS: Tuple[str, ...] = (
    "预约", "挂号", "取消", "退号", "退诊", "号源", "放号", "约号",
    "哪位医生", "哪些医生", "哪些专家", "哪位专家", "专家门诊", "医生名单",
    "帮我订", "预约一下", "订一个",
)

_OPERATION_RE = re.compile("|".join(_OPERATION_PATTERNS))


def is_operation_request(text: str) -> bool:
    """是否是操作类请求（挂号 / 取消 / 查号源 / 查专家）"""
    if not text:
        return False
    return bool(_OPERATION_RE.search(text))


def should_inject_knowledge(text: str) -> bool:
    """是否应该注入 RAG 知识"""
    return not is_operation_request(text)
