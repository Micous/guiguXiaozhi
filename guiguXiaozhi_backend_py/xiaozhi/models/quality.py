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
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 模型没调工具却声称完成了写操作（Java 版实测：temperature 高时会出现）
_FALSE_CONFIRMATION = re.compile(
    r"已(经)?(为您|替您|帮你|替你)?(成功)?(预约|挂号)"
    r"|预约(已经)?(成功|完成|提交成功)"
    r"|挂号(成功|已完成)"
    r"|已(成功)?取消(预约|挂号)"
    r"|预约号[:：]\s*[A-Za-z0-9-]{1,20}"
    # 2026-11-10 事故新增：「我为您预约了…以下是预约信息」——不带"成功/已"字样
    # 的完成态声明，原来的正则全部漏报（工具失败后模型仍谎报预约成功）。
    r"|[为帮](您|你)(成功)?预约了"
    r"|预约好了(?![吗么再之])"
    r"|挂号好了(?![吗么再之])"
    # 2026-11-10 第二次事故新增：「您的预约已成功安排在…」——
    # "预约"与"成功"之间隔着"已"，原正则的 `预约(已经)?(成功|…)` 只认"已经"，
    # 于是漏报，模型把一次**失败**的预约说成了成功。
    r"|预约(?:已经|已)?(?:成功|完成|办理成功|安排|提交)"
    r"|已(?:经)?(?:成功|完成)[^。！？\n]{0,6}(?:预约|挂号|安排|提交|办理)"
    # 2026-11-10 第三次事故新增：模型**伪造工具返回结果**
    # 「预约挂号工具返回结果：【成功】…」——整段都是编的，必须一起拦掉。
    r"|工具(?:调用)?(?:返回|执行)[^。！？\n]{0,10}(?:结果)?[：:【\s]*(?:【)?成功"
)

# 取消类承诺单独处理：真实联调里模型会写「您的预约已成功取消」，
# 语序与「已取消预约」相反，原来的正则匹配不到（B1 端到端发现的漏报）。
# 只匹配「完成态」措辞，不匹配「我想取消预约 / 如何取消预约」这类意图或疑问句。
_CANCEL_CONFIRMATION = re.compile(
    r"已(?:经)?(?:为您|替您|帮你|替你)?(?:成功)?取消(?:了)?"
    r"(?:预约|挂号|该预约|本次预约|这次预约)?"
    r"|(?:预约|挂号)(?:已经)?(?:已)?(?:成功)?取消(?:了|成功)?"
    r"|取消(?:预约|挂号)?(?:已经)?(?:成功|完成|办理成功)"
    r"|(?:成功|已经)(?:为您|替您|帮你)?取消(?:了)?(?:预约|挂号)?"
)

# 工具调用被当成纯文本输出。
#
# 小模型有两种吐法，**两种都要防**：
#   A. 工具名在前：「预约挂号 {"username": "张三"}」
#   B. JSON 在前  ：「{"name": "预约挂号", "arguments": {"username": "张三"}}」
#
# 2026-11-10 第五轮事故：只防住了 A，模型改用 B 之后，
# **原始 JSON 直接漏给用户**（用户看到一坨参数），且工具一次都没执行。
# 教训：防一种格式 ≠ 防住这个问题，枚举要按"模型实际会吐什么"来，不是按自己想得到的。
_TOOL_NAMES_PATTERN = r"(预约挂号|取消预约|查询是否有号源|查询科室专家)"

_TEXTUAL_TOOL_CALL = re.compile(
    # A 格式：工具名在前，后跟 JSON
    _TOOL_NAMES_PATTERN + r"\s*[{\n\r]"
    # B 格式：JSON 在前，里面带 name 字段（引号可能是英文/中文/无）
    r"|\{\s*[\"\u201c\u2018']?name[\"\u201d\u2019']?\s*:\s*"
    r"[\"\u201c\u2018']?" + _TOOL_NAMES_PATTERN
)

# 模型**用文字描述**自己要调工具，但同一轮并没有真的发出 tool_call。
#
# 2026-11-10 真实前端联调实测（trace ba096001...）：
#   模型输出「…现在我将为您提交预约请求。请稍等，我将调用预约挂号工具。」
#   同一轮 tool_call_count = 0 → 引擎按"没有工具调用 → 本轮结束"处理，
#   用户看到一句"我这就调用工具"，然后**什么都没发生**，工具一次都没执行。
# 这类"口头承诺"必须触发一次纠正重试，逼模型真的把工具调出来。
_NARRATED_TOOL_CALL = re.compile(
    r"(?:调用|使用|发起|执行|提交)[^。！？\n]{0,12}工具"
    r"|(?:调用|使用)[^。！？\n]{0,10}(?:预约挂号|取消预约挂号|查询是否有号源|查询科室专家)"
    r"|(?:我将|我这就|马上|稍后|接下来)[^。！？\n]{0,16}"
    r"(?:为您)?(?:提交|发起|办理|执行|查询)[^。！？\n]{0,16}"
    r"(?:预约请求|挂号请求|预约|挂号|取消预约|查询|号源)"
)

# 没调查询工具却**报告查询结果**（"查询结果显示，内科没有号源"）。
# 2026-11-10 第五轮联调实测：模型口头说"我将查询…"，然后直接编出
# "查询结果显示，2026年11月11日内科暂时没有可预约的号源"——同一轮零工具调用。
# 这类谎报比"承诺要调"更恶劣：用户会以为号源真的查过了。
# 措辞刻意不用裸的"没有号源"——"有没有号源吗"会误伤
# （有没有号源 包含 没有号源 子串），疑问语境另有 _is_conditional_context 兜底。
_QUERY_RESULT_CLAIM = re.compile(
    r"查询结果显示|查询结果[：为]"
    r"|没有可预约的号源"
    r"|号源已(全部)?约满"
    r"|还有?\s*\d+\s*个号"
)

# 兜底提示：不下结论说预约失败，只提醒以系统记录为准
FALSE_CONFIRMATION_NOTICE = (
    "\n\n（提示：本次未检测到对应的工具执行记录，请以系统查询结果为准，"
    "不要视为预约或取消已办理成功。）"
)

# 「把工具调用写成纯文本 JSON」的纠正提示。
#
# 实测（qwen2.5:7b）：模型有时不发出结构化 tool_call，而是把
# `预约挂号 {"username": "张三", ...}` 直接当正文吐出来。
# 后果有二：
#   1. **原始 JSON 直接漏给用户**（前端显示成一坨乱码般的参数）；
#   2. 工具一次都没执行 → 用户以为在办事，其实什么都没发生。
# 所以这类文本必须**拦在下发之前**，并纠正重试逼模型发出真正的 tool_call。
CORRECTIVE_PROMPT_TOOL_CALL = (
    "（系统要求）你刚才把工具调用写成了普通文本，参数不会被执行。"
    "请立即以**结构化工具调用**的方式发起调用（不要在正文里写工具名或 JSON），"
    "把参数如实填进去；不要输出任何说明性文字。"
    "只有工具真正执行后才会产生记录。如果工具返回失败，请如实把失败原因告诉用户。"
)

# 幻觉纠正提示：检测到"没调工具却声称成功"时注入一轮，逼迫模型真正调工具。
#
# 措辞要点（都是实测踩出来的）：
#   * **不要复述"你声称成功"** —— 旧措辞会把"已为您预约成功"这句话再说一遍，
#     7B 模型顺着这句话继续编，纠正重试等于白做；
#   * **必须点名具体工具** —— 旧措辞写"预约挂号 / 取消预约挂号"，模型分不清该调哪个，
#     于是干脆不调；
#   * **明确要求"只调用工具、不要输出文字"** —— 模型的自然倾向是先说话再干活。
CORRECTIVE_PROMPT_BOOK = (
    "（系统要求）本次请求需要完成一次挂号操作。"
    "请立即调用“预约挂号”工具，把姓名、身份证号、科室、日期、时段如实填入参数。"
    "不要输出任何结论性文字，也不要描述预约结果——只有工具真正执行后才会产生预约记录。"
    "如果工具返回失败，请如实把失败原因告诉用户。"
)

CORRECTIVE_PROMPT_CANCEL = (
    "（系统要求）本次请求需要取消一次挂号。"
    "请立即调用“取消预约挂号”工具，把姓名、身份证号、科室、日期、时段如实填入参数。"
    "不要输出任何结论性文字，也不要描述取消结果——只有工具真正执行后才会产生记录变更。"
    "如果工具返回失败，请如实把失败原因告诉用户。"
)

# "我这就调工具"但其实没调 —— 纠正提示要点是"不要描述，直接调"
CORRECTIVE_PROMPT_ACTION = (
    "（系统要求）你刚才只是**描述**了将要执行的动作，但并没有真正发起工具调用。"
    "用户看不到任何执行结果。请立即调用对应的工具完成这次操作，"
    "不要输出任何结论性文字，也不要描述你将要做什么——"
    "只有工具真正执行后才会产生记录。如果工具返回失败，请如实把失败原因告诉用户。"
)

CORRECTIVE_PROMPT = CORRECTIVE_PROMPT_BOOK  # 兼容旧引用

# 连续两次都编造成功时的兜底：只陈述事实，不给成功结论
FALLBACK_BOOKING_NOT_DONE = (
    "抱歉，本次预约**没有办理成功**——系统没有生成对应的预约记录。"
    "可能是号源刚刚被占满，请稍后重试，或换一个日期 / 时段再试。"
)

FALLBACK_CANCEL_NOT_DONE = (
    "抱歉，本次取消**没有执行成功**——系统没有找到可取消的预约记录或删除失败。"
    "请核对姓名、身份证号、科室、日期与时段后重试。"
)

FALLBACK_QUERY_NOT_EXECUTED = (
    "抱歉，本次查询没有实际执行——系统没有生成查询结果，"
    "刚才的号源信息不可信。请告诉我科室、日期和时段，我会重新为您查询。"
)


# 条件/疑问语境标记：出现在承诺**之前**时，说明这不是"已完成"的断言，
# 而是"如果…还没预约成功…"这类指导语，不该被当成幻觉拦截。
# 误判的代价：把一段正常的指导文字拦腰截断（2026-11-10 实测踩到）。
_CONDITIONAL_MARKERS: Tuple[str, ...] = (
    "没有", "还没", "未", "不", "若", "如果", "假如", "能否", "是否",
    "请问", "会不会", "有没有", "尚未", "并未", "并没", "需不需要",
    "要不要", "可以", "还没能", "失败", "无法",
)


def _is_conditional_context(text: str, start: int) -> bool:
    """承诺前文是否是条件/疑问语境（是 → 不算完成态断言）"""
    if start <= 0:
        return False
    window = text[max(0, start - 10) : start]
    return any(marker in window for marker in _CONDITIONAL_MARKERS)


def claim_span(text: str) -> Optional[Tuple[str, int, int]]:
    """找出文本里**最早**的一处成功承诺，返回 (类型, 起点, 终点)。

    与 ``claim_kind`` 的区别：这里给出位置，流式输出时可以**精确定位并截断**
    承诺所在的句子 —— 事后追加免责提示收不回已经显示给用户的字，
    只能在承诺下发之前拦（2026-11-10 事故的直接教训）。

    只返回**第一处非条件语境**的承诺：像"如果没有预约成功，请…"这种
    指导语不是完成态断言，不能拦（否则会把正常回答拦腰截断）。

    Returns:
        ("book"|"cancel"|"query", start, end) 或 None
    """
    if not text:
        return None
    best: Optional[Tuple[str, int, int]] = None
    for kind, regex in (
        ("cancel", _CANCEL_CONFIRMATION),
        ("book", _FALSE_CONFIRMATION),
        ("query", _QUERY_RESULT_CLAIM),
    ):
        for match in regex.finditer(text):
            if _is_conditional_context(text, match.start()):
                continue
            if best is None or match.start() < best[1]:
                best = (kind, match.start(), match.end())
    return best


def claim_kind(text: str) -> Optional[str]:
    """判断文本里的成功承诺属于哪类操作。

    Returns:
        "book" / "cancel" / "query" / None
    """
    span = claim_span(text)
    return span[0] if span else None


def corrective_prompt(kind: Optional[str]) -> str:
    """按操作类型给出针对性的纠正提示"""
    if kind == "cancel":
        return CORRECTIVE_PROMPT_CANCEL
    if kind == "action":
        return CORRECTIVE_PROMPT_ACTION
    if kind == "tool_call":
        return CORRECTIVE_PROMPT_TOOL_CALL
    if kind == "query":
        # 编造查询结果 → 用通用的"别描述、真调工具"纠正提示
        return CORRECTIVE_PROMPT_ACTION
    return CORRECTIVE_PROMPT_BOOK


# 工具失败类型 → 面向用户的说明。
# 兜底文案必须**反映工具的真实反馈**，而不是笼统地说"没有生成记录"——
# 用户明明已经约过号（工具返回"已有预约，请勿重复预约"）时说"没有生成记录"
# 同样是误导。
_FAILURE_HINTS: Dict[str, str] = {
    "DuplicateAppointment": "您在同一科室、同一日期、同一时段已经有一条预约记录了，请勿重复预约。",
    "NoSlotAvailable": "该医生在您选择的日期与时段不出诊，或号源已被约满。建议换一个日期、时段或医生。",
    "ValidationError": "预约信息没有填写完整或格式有误，请核对姓名、身份证号、科室、日期和时段后重试。",
    "PersistenceError": "预约记录写入失败，请稍后重试。",
    "ApprovalRequired": "这次操作需要用户确认，但当前没有确认通道，请稍后重试。",
    "PermissionDenied": "该操作被安全策略禁止，无法执行。",
    "ToolNotFound": "所需的工具不存在，请稍后重试。",
}


def fallback_for(
    kind: Optional[str],
    tool_failure: Optional[Dict[str, Any]] = None,
) -> str:
    """纠正重试仍失败时的兜底文案。

    ``tool_failure`` 是本请求内最近一次工具失败的信息
    （``{"tool":…, "error_type":…, "content":…}``）。
    有它就按工具的真实反馈说明原因，没有才用通用文案。
    """
    if kind == "cancel":
        base = FALLBACK_CANCEL_NOT_DONE
    elif kind == "query":
        base = FALLBACK_QUERY_NOT_EXECUTED
    else:
        base = FALLBACK_BOOKING_NOT_DONE

    if not tool_failure:
        return base
    error_type = str(tool_failure.get("error_type") or "")
    hint = _FAILURE_HINTS.get(error_type)
    if not hint:
        return base
    return f"抱歉，本次操作没有完成——{hint}"


def booking_executed_in_history(history: Sequence[Any]) -> bool:
    """历史会话里是否真的**成功**执行过预约挂号（且之后未被取消）。

    用途有二：
    * 抑制误报——用户在**上一个请求**里成功挂号后问"挂号成功了吗？"，
      模型回答"已经预约好了"是真话，不应追加免责提示，也不应纠正重试；
    * 纠正重试的前提——历史里从未成功执行过预约工具，才能逼迫模型调工具
      （否则可能造成重复预约，虽然工具层已有 find_duplicate 兜底）。

    判定依据是历史消息上的 ``metadata`` 标记（engine 在工具真实执行成功后
    打在持久化的 assistant 消息上）：``booking_executed`` / ``cancel_executed``。

    **不能只看 tool_calls**：2026-11-10 事故里，三次失败的预约调用
    （日期已过、参数类型错）的 tool_calls 都进了历史，裸看 tool_calls 会把
    "失败"当成"已预约"，进而把幻觉检测整体抑制——模型谎报"预约已经成功"
    却没有任何拦截。没有标记的历史（旧会话）一律视为未预约：宁可信其无，
    由纠正重试兜底（工具层 find_duplicate 防重复预约）。
    """
    booked = False
    for message in history or []:
        meta = getattr(message, "metadata", None) or {}
        if meta.get("cancel_executed"):
            booked = False
        elif meta.get("booking_executed"):
            booked = True
    return booked


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
    """是否在没有任何工具调用的情况下声称已预约或已取消成功。

    ``tool_executed`` 是**请求级**标志：工具执行后模型会再生成一轮总结文本，
    那一轮本身没有 tool_calls，但只要本请求内真的执行过工具，就不能算幻觉。
    """
    if has_tool_calls or tool_executed:
        return False
    text = text or ""
    return bool(
        _FALSE_CONFIRMATION.search(text) or _CANCEL_CONFIRMATION.search(text)
    )


def detect_textual_tool_call(text: str) -> bool:
    """工具调用被改写成纯文本输出"""
    return bool(_TEXTUAL_TOOL_CALL.search(text or ""))


def textual_tool_call_span(text: str) -> Optional[Tuple[str, int, int]]:
    """找出纯文本形式工具调用的位置，返回 (起点, 终点)。

    流式拦截要用它来**精确切掉**那一整段 —— 只知道"有"是不够的，
    切割点不对就会把参数漏给用户。

    切法分两种：
      * A 格式（工具名在前）：从工具名切到 **JSON 块结束**；
      * B 格式（JSON 在前）  ：从 ``{`` 切到 **JSON 块结束**。

    "JSON 块结束"的判定：从起始的 ``{`` 开始做**括号配对**，
    跳过字符串字面量里的括号。这样 ``{"arguments": {"a": "}"}}`` 也能正确收尾。
    """
    text = text or ""
    match = _TEXTUAL_TOOL_CALL.search(text)
    if match is None:
        return None

    # B 格式：JSON 在前 → 从 match.start()（那个 "{"）开始配对
    if text[match.start() : match.start() + 1] == "{":
        start = match.start()
    else:
        # A 格式：工具名在前 → 工具名本身也不该给用户看，
        # 所以从工具名处切，JSON 块整体丢弃
        start = match.start()

    if text[start : start + 1] == "{":
        end = _json_block_end(text, start)
    else:
        # A 格式：切掉工具名 + 紧随其后的 JSON 块
        brace = text.find("{", start)
        end = _json_block_end(text, brace) if brace >= 0 else match.end()
    return (start, end)


def _json_block_end(text: str, start: int) -> int:
    """从 ``text[start] == '{'`` 开始做括号配对，返回该 JSON 块的结束位置（不含）。

    需要跳过字符串字面量内部的括号（``{"a": "}"}``）。
    找不到配对的 ``}`` 时返回 len(text) —— 流式场景下 JSON 还没吐完，
    此时应当把**剩余全部**都拦掉，等下一轮再判断。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    # 没配平：JSON 未吐完，整段拦掉
    return len(text)


def detect_narrated_tool_call(text: str) -> bool:
    """模型只是**说要**调工具，却在同一轮没有真的发出 tool_call。

    与 ``detect_textual_tool_call`` 的区别：
      * textual —— 把工具调用**写成了 JSON 文本**；
      * narrated —— 用自然语言**承诺**要调工具（"我将调用预约挂号工具"）。

    两者都会导致工具一次都没执行，用户却以为系统在办事。
    """
    return bool(_NARRATED_TOOL_CALL.search(text or ""))


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
