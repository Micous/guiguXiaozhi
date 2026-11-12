"""预约科室确定性守卫 —— 院方名录是唯一真值，不能交给 7B 转述。

真机事故（2026-11-12，trace 01de88f0 / f87ff2b6）：
用户填单「王新勃，610121199401032831，内科，2026.11.14」，本院并无"内科"。
模型先调了「查询是否有号源」，工具明确回了
「本院没有【内科】这个科室，请先向用户说明…等用户确认后再查询」，
7B 却**完全无视 ToolResult**，继续要时段、承诺"我将为您预约"；
用户补「上午」后审批确认窗弹出（确认的是一组注定失败的参数），
带 token 重发那一轮更是 4 次 llm.chat / 43 秒 / 3 次虚假承诺截断 /
2 次纠正重试，最后才把"没有内科"转述出来。

结论与 Phase 7 医疗安全、1.6 问候快路径一致：**确定性事实必须短路**，
不能指望提示词或小模型转述工具返回值。本模块只负责"判定 + 生成话术"，
下发/落库/指标由 engine_phases 的短路方法完成。

判定收紧在**预约填单语境**：含身份证号或预约/挂号动作词，且文本里
找不到任何本院真实科室名，却出现了一个科室样词。科室咨询、科普问答、
急症主诉、纯问候都不在本模块职责内（分别由系统提示词权威名单、
医疗安全短路、问候快路径处理）。

名单为空时一律放行 —— HIS 主档不可用不能伪装成"本院没有科室"
（his_doctor_service 的 fail-fast 教训）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from xiaozhi.core.message import Role
from xiaozhi.security.validators import validate_department

# 结构化填单常见分隔符；日期里的 "." 不切，避免把 2026.11.14 切成碎片
_SPLIT_RE = re.compile(r"[,，、;；:：\s|/。!！?？()（）\[\]【】]+")
_ID_CARD_RE = re.compile(r"\d{15,}")
_APPOINTMENT_RE = re.compile(r"预约|挂号|挂[个一科]|帮我挂|想挂|帮我约|约个号")
# 科室样词：1~10 个汉字 + "科"/"门诊"。「内科」「耳鼻咽喉头颈外科」都能覆盖
_DEPT_WORD_RE = re.compile(r"[\u4e00-\u9fa5]{1,10}(?:科|门诊)")
_VERB_PREFIX_RE = re.compile(
    r"^(?:我想|我要|帮我|麻烦|请|想|要|去|到|挂|约|预约|挂号|看|咨询|查询|查|选|定|一个|个|的|了)+"
)
_TAIL_RE = re.compile(r"(?:的号|号源|号|吧|呢|啊|嘛|呀|一下|挂号|预约)+$")
_DEPT_SUFFIX_RE = re.compile(r"(?:专家门诊|普通门诊|门诊)$")


@dataclass(frozen=True)
class DepartmentGuardVerdict:
    """命中守卫：mentioned 是用户说的那个科室，reply 是要下发的固定话术。"""

    mentioned: str
    reply: str
    suggestions: Tuple[str, ...] = ()


def _clean_known(known: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in known or []:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return sorted(out)


def _extract_mentions(text: str) -> List[str]:
    """从填单字段或自由文本里取出"看起来是科室"的词。

    先按分隔符切段，再剥掉动词前缀与「的号/号源」尾巴，最后取段内第一个
    科室样词。日期、身份证号、姓名段都不会产生科室样词。
    """
    mentions: List[str] = []
    for segment in _SPLIT_RE.split(text or ""):
        segment = segment.strip()
        if not segment or _ID_CARD_RE.search(segment):
            continue
        cleaned = _VERB_PREFIX_RE.sub("", segment).strip()
        cleaned = _TAIL_RE.sub("", cleaned).strip()
        if not cleaned:
            continue
        match = _DEPT_WORD_RE.search(cleaned)
        if match:
            word = match.group(0)
            if word not in mentions:
                mentions.append(word)
    return mentions


def _suggestions(mentioned: str, known: List[str]) -> List[str]:
    """给出同亚专科候选：'内科' → 呼吸内科/心内科/消化内科/神经内科。"""
    direct = [k for k in known if k.endswith(mentioned)]
    if direct:
        return direct[:6]
    if len(mentioned) >= 2:
        tail = mentioned[-2:]
        return [k for k in known if k.endswith(tail)][:6]
    return []


def _build_reply(mentioned: str, known: List[str], suggestions: List[str]) -> str:
    lines = [f"很抱歉，本院没有【{mentioned}】这个科室。"]
    lines.append("本院开设的科室有：" + "、".join(known) + "。")
    if suggestions:
        lines.append("与它相关的有：" + "、".join(suggestions) + "。")
    lines.append("请告诉我您想预约其中哪个科室，我再帮您继续。")
    return "\n".join(lines)


def assess_department_guard(
    text: str, known_departments: Sequence[str]
) -> Optional[DepartmentGuardVerdict]:
    """预约语境下发现本院不存在的科室 → 返回短路话术；否则返回 None 放行。

    放行（返回 None）的四种情况必须守住，否则会误伤正常对话：
      * HIS 主档为空/不可用；
      * 不是预约语境（科室咨询、科普、急症、问候）；
      * 文本里出现了任一本院真实科室名（用户已经选对）；
      * 没提取到科室样词，或提取到的词通过了工具层同一套校验。
    """
    known = _clean_known(known_departments)
    if not known:
        return None
    raw = (text or "").strip()
    if not raw or not (_ID_CARD_RE.search(raw) or _APPOINTMENT_RE.search(raw)):
        return None
    # 用户已经给出合法科室（含"我要约呼吸内科，不是内科"这类澄清）→ 放行
    if any(name in raw for name in known):
        return None
    mentions = _extract_mentions(raw)
    if not mentions:
        return None
    mentioned = mentions[0]
    # 与 BookAppointmentTool / QuerySlotTool 用同一套校验，避免两处判定漂移
    if validate_department(mentioned, known).ok:
        return None
    suggestions = _suggestions(mentioned, known)
    return DepartmentGuardVerdict(
        mentioned=mentioned,
        reply=_build_reply(mentioned, known, suggestions),
        suggestions=tuple(suggestions),
    )


# ==========================================================================
# 掩码还原：落库/展示用掩码，**执行**必须用真值
# ==========================================================================
#
# 真机事故（2026-11-12，用户贴的完整对话）：用户首轮给的是完整身份证号，
# 落库时 assistant 的叙述与 tool_call 参数按 PII 契约被打码成
# ``610121********2831``；下一轮用户只说"下午"，7B 从历史里照抄掩码当参数，
# ``validate_appointment`` 报「身份证号格式有误，应为18位」，预约永远约不成。
#
# 掩码是**存储与展示**口径，不能变成**执行**口径。真值只在用户原文里
# （PII 契约明确"不改用户消息"，user 消息保持完整），所以执行前从当前输入
# 或历史 user 消息取最近一次完整身份证号还原。

# 完整身份证号：18 位（末位可为 X）或 15 位旧号；前后不能再接数字，
# 避免从更长数字串里截一段。
_ID_CARD_TRUTH_RE = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)")
_MASK_CHARS = ("*", "＊")
_ID_CARD_KEY_PARTS = ("id_card", "idcard", "identity", "身份证")


def extract_latest_id_card(
    history: Optional[Sequence[Any]], user_input: str = ""
) -> str:
    """取「用户最近一次提供的完整身份证号」作为执行真值。

    顺序：当前轮输入 → 历史 user 消息倒序。模型产出（assistant/tool）一律
    跳过——那里的身份证按契约是掩码，取回来只会把掩码当真值。
    找不到返回空串（调用方据此跳过还原，让校验失败并如实提示用户）。
    """
    candidates: List[str] = [user_input or ""]
    for message in reversed(list(history or [])):
        if getattr(message, "role", None) == Role.USER:
            candidates.append(getattr(message, "content", "") or "")
    for text in candidates:
        found = _ID_CARD_TRUTH_RE.findall(text)
        if found:
            return found[-1]
    return ""


def is_masked_value(value: Any) -> bool:
    """值是否已被掩码（含星号）。只有掩码值需要还原，真实值原样保留。"""
    text = str(value or "")
    return any(ch in text for ch in _MASK_CHARS)


def restore_masked_id_cards(
    args: Dict[str, Any], known_id_card: str
) -> Tuple[Dict[str, Any], bool]:
    """把工具参数里**被掩码的**身份证字段还原成真值。

    只在值为掩码（含 ``*``）时替换：模型自己给的合法真值不动，避免无谓改写
    与审计噪音。返回 ``(新参数字典, 是否发生还原)``，不原地修改入参。
    """
    if not args or not known_id_card:
        return args, False
    restored = dict(args)
    changed = False
    for key, value in list(restored.items()):
        if not isinstance(value, str):
            continue
        lowered = str(key).lower()
        if not any(token in lowered for token in _ID_CARD_KEY_PARTS):
            continue
        if is_masked_value(value):
            restored[key] = known_id_card
            changed = True
    return restored, changed
