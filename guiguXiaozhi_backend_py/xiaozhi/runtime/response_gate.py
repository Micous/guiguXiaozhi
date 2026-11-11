"""写操作 Response Gate —— Phase 4 / P0-05 的**确定性**兜底层。

审计 P0-05 的目标规则：写操作请求（预约/取消/退号/含患者身份信息的操作）里，
完成态表述（"已预约成功/已取消"）只能来自**本请求真实执行且持久化成功**的
业务结果，不能由模型自由发挥；UNKNOWN 只能表达"结果核实中"。

与既有防线的关系（纵深防御，互不替代）：
    * StreamFilter（流内截断）      —— 承诺**下发前**逐字拦截，仍是第一道；
    * 拦截器链 + 纠正重试           —— 漏判句式的第二道（逼模型真调工具）；
    * 本 Gate（本模块）             —— **确定性**收口：判据不靠正则枚举模型说法，
      只看「本请求是否真的执行过写操作」这一业务事实。

为什么写操作模式下 ``claim_is_legit`` **不再信任 prior_booking**（历史真挂过号）：
    2026-11-11 真机实测（8.4 第 6 条残留）：用户不带 token 重发同一句"帮我预约…"，
    历史里有成功记录 → ``claim_is_legit`` 判真 → 模型复述"预约成功"原样下发，
    而本次请求**什么都没做**。查询类请求（"挂号成功了吗"）不受影响——
    那是 `is_status_inquiry`，根本不进写模式。

设计取舍（与路线图原文"自由文本进缓冲区"的差异）：
    不缓冲全部模型文本，只对**完成态承诺**做确定性门控。原因：
    1) 叙述段是 NarratedToolCallInterceptor 的检测输入，且特征测试
       （E1）钉住了"无害前缀正常流式下发"的 UX；
    2) 完成态承诺的流内截断 + 本 Gate 的请求级兜底，合计已满足
       审计 P0-05 的 6 条验收（tests/unit/test_response_gate.py 逐条对号）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from xiaozhi.models.quality import claim_kind, fallback_for
from xiaozhi.runtime.intent import is_short_confirmation, is_status_inquiry

# 写操作意图：预约/挂号/取消/退号类动词。
# 与 intent.py 的 _OPERATION_PATTERNS 的区别：这里只要**写**意图，
# 查询类（"查号源"/"哪些专家"）不进写模式——读取类请求继续流式输出。
_WRITE_INTENT_RE = re.compile(r"预约|挂号|取消|退号|退诊|约号|帮我订|订一个")

# 取消/退号是**祈使动作**（"取消我的预约"）；但"取消成功了吗 / 取消结果如何"
# 是在问既有结果。两者用疑问标记区分（intent.py 的 _STATUS_INQUIRY_PATTERNS
# 里"我的预约"这类宽模式会把祈使句误判成查询，必须先于它做祈使判定）。
_CANCEL_ACTION_RE = re.compile(r"取消|退号|退诊|撤销")
_RESULT_QUESTION_RE = re.compile(r"成功了吗|成功没有|成功没|结果|办好了吗|了吗|么样|如何")

# 身份证号（15/18 位，含尾部 X）。中文与数字之间不存在 \b 词边界，禁用 \b。
_ID_CARD_RE = re.compile(r"\d{15,18}X?|\d{17}[\dXx]")

# UNKNOWN 结果的固定文案（审计 P0-05 目标规则原文）。
UNKNOWN_NOTICE = "结果核实中，请勿重复提交。"


def is_write_operation(user_input: str, last_assistant_text: str = "") -> bool:
    """是否进入 WRITE_OPERATION 输出模式（写操作请求）。

    判定必须**带上下文**（与 intent.is_operation_request 同一教训）：
    模型问"需要我帮您预约吗"，用户回"需要" —— 短确认句本身没有写关键词，
    但它是写操作的延续。
    """
    if not user_input:
        return False
    # 取消/退号祈使句（"取消我的预约"）：不含疑问标记 → 写操作。
    # 必须在 is_status_inquiry 之前判定 —— 后者的"我的预约"等宽模式
    # 会把祈使句误判成查询。
    if _CANCEL_ACTION_RE.search(user_input) and not _RESULT_QUESTION_RE.search(
        user_input
    ):
        return True
    # 询问既有操作结果（"挂号成功了吗"）不是发起写操作，可以引用历史事实
    if is_status_inquiry(user_input):
        return False
    if _WRITE_INTENT_RE.search(user_input) or _ID_CARD_RE.search(user_input):
        return True
    # 短确认句承接上一轮的写操作提议
    if (
        is_short_confirmation(user_input)
        and last_assistant_text
        and (
            _WRITE_INTENT_RE.search(last_assistant_text)
            or _ID_CARD_RE.search(last_assistant_text)
        )
    ):
        return True
    return False


def stream_claim_is_legit(state: Any) -> bool:
    """写操作模式下，成功承诺是否可信（供 StreamFilter / inspect_turn 用）。

    **只认本请求**真实执行成功的写操作。历史里的 booking_executed
    （prior_booking）在写模式下不为承诺背书 —— 本次请求没做事，
    就不能让任何"已预约成功"流出去（真机残留：重发同一句复述成功）。
    """
    return state.action_executed_success


def curate_final_reply(
    state: Any, final_text: str
) -> Tuple[str, Optional[str]]:
    """请求收尾的确定性门控。返回 (最终文本, 替换原因)。

    替换原因 None 表示放行。规则（按优先级）：
      1. 工具报告 UNKNOWN → 固定文案"结果核实中，请勿重复提交"
         （不得宣称成功或失败——审计 P0-05 验收 3/6）；
      2. 本请求写操作执行成功 → 业务事实支持完成态，模型转述放行
         （验收 4）；
      3. 无写执行但文本含完成态承诺 → 按 claim 类型 + 工具真实失败原因
         生成确定性兜底文案**整段替换**（验收 1/2/5/6 —— 纠正重试漏掉的
         换皮句式在这里被兜住，而不是只追加免责声明）；
      4. 其余（问科室、问日期等无承诺文本）放行。
    """
    text = final_text or ""
    # 1. UNKNOWN：结构化结果说了算
    if getattr(state, "has_unknown_write", False):
        return UNKNOWN_NOTICE, "unknown_write"
    # 2. 本请求真的写成功 → 放行
    if getattr(state, "write_executed_success", False):
        return text, None
    # 3. 无写执行 → 完成态承诺一律替换
    kind = claim_kind(text)
    if kind:
        return fallback_for(kind, state.last_tool_failure), f"claim_blocked:{kind}"
    # 4. 无承诺 → 放行（问询/澄清类文本）
    return text, None
