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
# 注意后两条是正则片段（整串用 "|" 连接编译）：
# * 身份证号（15/18 位数字，含尾部 X）——用户按模型上一轮的追问补全挂号信息时，
#   消息里往往只有"姓名，身份证号…，日期"而没有"预约"字样
#   （2026-11-10 真实前端联调实测：这条消息被当成知识问答，注入知识 +
#   不中性化历史，模型随即口头调用工具、编造预约成功）；
#   注意不能用 \b 边界——中文属于 \w，"号110101…"之间不存在词边界。
# * "推荐一位专家"这类请求应路由到查询科室专家工具，而不是 RAG 答题。
_OPERATION_PATTERNS: Tuple[str, ...] = (
    "预约", "挂号", "取消", "退号", "退诊", "号源", "放号", "约号",
    "哪位医生", "哪些医生", "哪些专家", "哪位专家", "专家门诊", "医生名单",
    "帮我订", "预约一下", "订一个",
    r"推荐.{0,6}(?:专家|医生)", r"(?:专家|医生).{0,4}推荐",
    r"\d{15,}",
)

_OPERATION_RE = re.compile("|".join(_OPERATION_PATTERNS))


# ---------------------------------------------------------------------------
# 短确认句（"需要" / "好的" / "确认" …）
#
# 2026-11-10 真实前端联调挖出的**第三层**意图盲区：
# 助手问"请问您是否需要我帮您预约其中一位医生？"，用户回"需要" ——
# 这两个字里没有任何操作关键词，纯文本正则必然漏判：
#   * 漏判成知识问答 → 注入 RAG → 工具调用率归零（实测 5/5 → 0/5）；
#   * 漏判成非操作类 → 历史不做中性化 → 模型照抄历史里的"预约成功啦"。
# 所以操作类判定必须**带上上下文**（上一轮助手说了什么）。
# ---------------------------------------------------------------------------
_CONFIRM_RE = re.compile(
    r"^(?:好的?|好|行|嗯+|是(?:的|啊)?|对|对啊|没错|可以|确认|确定|需要"
    r"|就这样|就这个|就那位|来吧|麻烦你(?:了)?|收到|明白|懂了|继续|那就|ok|okay|yes|y)"
    r"[，,。！!~、\s…]*$",
    re.IGNORECASE,
)


def is_short_confirmation(text: str) -> bool:
    """是否是**短确认/应答句**（用于衔接上一轮的操作提议）。

    限制长度 ≤ 16 字，避免把"我需要挂号"这类完整请求误判成确认句。
    """
    t = (text or "").strip()
    if not t or len(t) > 16:
        return False
    return bool(_CONFIRM_RE.match(t))


def mentions_operation(text: str) -> bool:
    """文本里是否在谈挂号/预约/取消/号源/专家这类操作"""
    if not text:
        return False
    return bool(_OPERATION_RE.search(text))


# 询问**既有**操作结果的句子（"挂号成功了吗" / "我的预约还在吗"）。
# 与"发起新操作"严格区分：前者可以引用历史里已完成的预约，
# 后者必须本轮真的调工具。
_STATUS_INQUIRY_PATTERNS: Tuple[str, ...] = (
    "成功了吗", "成功没有", "成功没", "成功否", "怎么样", "好了吗", "办好了吗",
    "结果如何", "预约结果", "挂号结果", "取消结果", "我的预约", "我的挂号",
    "我的号", "查一下我的预约", "查我的预约", "预约信息", "挂号信息",
    "预约了吗", "挂号了吗", "约上了吗", "有没有约上", "还预约着吗",
    "还在吗", "预约记录", "挂号记录", "预约信息还在",
)


def is_status_inquiry(text: str) -> bool:
    """是否是**询问既有操作结果**（而不是发起一次新的操作）"""
    if not text:
        return False
    return any(p in text for p in _STATUS_INQUIRY_PATTERNS)


def is_operation_request(text: str, last_assistant_text: str = "") -> bool:
    """是否是操作类请求（挂号 / 取消 / 查号源 / 查专家）

    Args:
        text:               用户当前输入
        last_assistant_text: 上一轮助手的回复（可选）。
                            用于识别"需要"这类**承接上一轮操作提议**的短确认句。
    """
    if not text:
        return False
    if _OPERATION_RE.search(text):
        return True
    # 短确认句 + 上一轮在谈操作 → 本轮是操作请求的延续
    if last_assistant_text and is_short_confirmation(text) and mentions_operation(
        last_assistant_text
    ):
        return True
    return False


def should_inject_knowledge(text: str, last_assistant_text: str = "") -> bool:
    """是否应该注入 RAG 知识"""
    return not is_operation_request(text, last_assistant_text)
