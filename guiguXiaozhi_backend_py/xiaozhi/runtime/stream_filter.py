"""流式下发过滤器 —— engine 重构 E3 的产物。

把 engine 里内嵌的流式文本处理（回看窗口、虚假承诺拦截、纯文本工具调用
切割）收敛成一个独立组件：`feed(delta)` 逐 chunk 喂入，返回"本轮可安全
下发"的文本（没有就返回空串）；`flush()` 在流结束时补发回看窗口里的余量。

**行为与旧内嵌实现逐字对齐**（E1 特征测试钉死）：
  * textual 检查先于 claim 检查（同一 delta 里两个都命中时 textual 优先）；
  * claim_is_legit（booking/cancel 真执行过）时两类拦截全部关闭；
  * query 类承诺单独看 `query_executed_success`（查询真执行过 → "查询结果
    显示…"是真话，放行）；
  * 拦截后进入 suppressed：后续内容一律不下发，但**继续读完流**
    （否则拿不到 tool_calls）；
  * 截断点前的无害前缀先裁到句子边界再补发（_trim_to_sentence）；
  * 回看窗口 LOOKBEHIND 个字押后下发，等承诺"露头"再决定拦不拦；
  * 条件语境（承诺前 10 字含"不/没有/是否"…）不拦截 —— 防指导语误伤
    的现行为，副作用也原样保留（见 test_lookbehind_conditional_context_is_not_truncated）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Tuple

from xiaozhi.models.quality import claim_span, textual_tool_call_span
from xiaozhi.ops.metrics import MetricsCollector

logger = logging.getLogger(__name__)

# 截断承诺时"承诺之前的前缀"会补发给用户。7B 高频先吐一串应承词
# （真机 2026-11-12：「好的，好的，好的，我将为您预约内科的号」），
# 承诺被丢掉后只剩「好的，好的，好的，」单独占一个气泡，看起来像刷屏。
# 纯应承/标点前缀不该下发；只要还剩一个实义词就原样保留，避免误删正文。
_FILLER_STRIP_RE = re.compile(
    r"[\s，,。.！!？?、；;：:…~\-—]+"
    r"|(?:好的|好嘞|好呀|好|嗯|哦|噢|哎|行|马上|请稍等|稍等|正在|我这就|这就|没问题|收到|明白|了解|请|您)"
)


def is_pure_filler(text: str) -> bool:
    """前缀是不是"停在半句的纯应承碎片"（该丢），而不是一句完整的话。

    **带句末标点的完整句一律保留**：既有特征测试钉死了
    「好的。+ 纯文本工具调用 JSON」被截断时前缀「好的。」必须下发。
    真机刷屏的「好的，好的，好的，」停在逗号、没有句末标点，才是要丢的。
    """
    value = text or ""
    if not value.strip():
        return True
    if any(ch in value for ch in "。！？!?\n"):
        return False
    return not _FILLER_STRIP_RE.sub("", value).strip()


# 流式下发的**回看窗口**（字）。
#
# 承诺正则要等匹配文本完整出现才命中，而命中点比承诺**起点**晚十几个字
# （"已为您预约成功"要等到"预约"出现才命中，起点却在更早的"已"）。
# 不留窗口就会先漏出"已"再被截断 —— 用户看到半截承诺。
# 窗口必须 >= 最长承诺正则的长度（当前最长约 20 字），取 40 留足余量。
LOOKBEHIND = 40

# 句子边界（截断时保留到这里为止，避免留下半截句子）
_SENTENCE_END = "。！？!?\n"
# 次级边界：没有完整句子时，裁到最近的逗号，至少保留已经说清的信息
_CLAUSE_END = "，,、;；"


def trim_to_sentence(text: str) -> str:
    """把文本裁到最后一个完整句子的边界。

    截断承诺时，承诺之前的前缀往往停在半句上
    （"很遗憾，您在2026年11月11日上午" + 兜底文案 → 读起来像病句）。

    裁剪策略：
      1. 有完整句子 → 裁到最后一个句末标点；
      2. 只有逗号分句 → 裁到最后一个逗号（保留已经说清的信息）；
      3. 连逗号都没有 → 整段丢弃（半句话比不完整更糟）。

    **保留标点**：裁出来的"很遗憾，"要带着逗号，
    后面接下一句时读起来才通顺（"很遗憾，抱歉，本次预约…"）。
    """
    if not text:
        return ""
    cut = -1
    for index, char in enumerate(text):
        if char in _SENTENCE_END:
            cut = index + 1
    if cut >= 0:
        return text[:cut].rstrip()
    for index, char in enumerate(text):
        if char in _CLAUSE_END:
            cut = index + 1
    if cut < 0:
        return ""
    return text[:cut].rstrip()


def safe_prefix(turn_buffer: str, full_text: str) -> str:
    """从完整输出里切出"实际下发给用户"的那一段（去掉被截断的承诺及其后续）。

    ``turn_buffer`` 是流式拼出来的完整文本，``full_text`` 是 holder 里的
    同一段文本；两者一致时直接按承诺起点裁剪，不一致时保守返回空串。

    裁剪后再裁到句子边界 —— 承诺前的半截句子对用户没有意义。
    """
    if not full_text:
        return ""
    if turn_buffer.startswith(full_text):
        span = claim_span(full_text)
        if span is None:
            return full_text
        return trim_to_sentence(full_text[: span[1]])
    # 兜底：holder 文本与流式缓冲不一致（理论上不该发生）→ 宁可丢也别漏
    return ""


class StreamFilter:
    """单轮流式输出的下发过滤（一次模型调用一个实例，不复用）。"""

    def __init__(
        self,
        *,
        claim_is_legit: bool,
        query_executed_success: bool,
        metrics: Optional[MetricsCollector] = None,
        trace_id: str = "",
    ) -> None:
        self.claim_is_legit = claim_is_legit
        self.query_executed_success = query_executed_success
        self.metrics = metrics
        self.trace_id = trace_id

        # 流式状态（与旧 engine 内嵌变量一一对应）
        self.turn_buffer = ""
        self.pending_start = 0
        self.suppressed = False
        self.truncated_claim: Optional[str] = None
        self.textual_tool_call = False

    # ---- 下发路径 ----

    def feed(self, delta: str) -> str:
        """喂入一个 delta，返回本轮可安全下发的文本（无则空串）。"""
        if not delta:
            return ""
        self.turn_buffer += delta
        if self.suppressed:
            # 承诺之后的内容一律不下发（但**继续读完流**，
            # 否则拿不到 tool_calls，工具就永远不会被执行）
            return ""

        # 「把工具调用写成纯文本 JSON」也要拦在下发之前：
        # 原始参数漏给用户是一方面，更关键的是
        # 工具一次都没执行 —— 用户以为在办事，其实什么都没发生。
        textual_span = (
            None if self.claim_is_legit else textual_tool_call_span(self.turn_buffer)
        )
        if textual_span is not None:
            self.textual_tool_call = True
            self.suppressed = True
            # 精确切掉"工具名 + JSON"这一段，
            # 切割点不对就会把参数漏给用户
            safe = trim_to_sentence(
                self.turn_buffer[self.pending_start : textual_span[0]]
            )
            self.pending_start = len(self.turn_buffer)
            if self.metrics is not None:
                self.metrics.inc("textual_tool_call_truncated")
            logger.warning(
                "模型把工具调用写成了纯文本，就地截断：trace=%s", self.trace_id
            )
            if is_pure_filler(safe):
                safe = ""
            return safe

        match = None if self.claim_is_legit else claim_span(self.turn_buffer)
        if match is not None and match[0] == "query" and self.query_executed_success:
            # 查询工具真的执行过 → "查询结果显示…"是真话
            match = None
        if match is not None:
            kind, start, _end = match
            self.truncated_claim = kind
            self.suppressed = True
            # 承诺之前的前缀无害，补发出去（承诺本身被丢掉）
            safe = trim_to_sentence(self.turn_buffer[self.pending_start : start])
            self.pending_start = len(self.turn_buffer)
            if self.metrics is not None:
                self.metrics.inc("false_confirmation_truncated")
            logger.warning(
                "模型输出中出现虚假成功承诺，就地截断：trace=%s kind=%s",
                self.trace_id,
                kind,
            )
            if is_pure_filler(safe):
                safe = ""
            return safe

        # 回看窗口：末尾 LOOKBEHIND 个字先留着不下发，
        # 等下一段文本来了再判断它是不是承诺的开头
        flush_upto = len(self.turn_buffer) - LOOKBEHIND
        if flush_upto > self.pending_start:
            chunk = self.turn_buffer[self.pending_start : flush_upto]
            self.pending_start = flush_upto
            return chunk
        return ""

    def flush(self) -> str:
        """流结束时补发回看窗口里剩下的字（被截断抑制时补发空串）。"""
        if not self.suppressed and self.pending_start < len(self.turn_buffer):
            chunk = self.turn_buffer[self.pending_start :]
            self.pending_start = len(self.turn_buffer)
            return chunk
        return ""

    # ---- 检测路径 ----

    def apply_truncation(self, candidate: Any) -> Any:
        """流结束后按截断状态修正候选回合文本。

        检测用**完整**文本（含被截断的承诺）；下发给用户 / 写进历史的是
        **裁剪后**的文本。两者分开，才不会因为"先把谎话删掉"而检测不到它。
        """
        if self.truncated_claim is not None:
            candidate.text = safe_prefix(self.turn_buffer, candidate.text)
        return candidate
