"""患者画像事实抽取器 —— P2-M1。

从会话消息里抽取可跨会话持久化的事实（姓名/身份证/科室偏好/过敏史/慢性病）。
两路抽取：
  * 规则路（确定性，零风险）：身份证号、姓名（"我叫X/我姓X/患者X"）、
    科室（对照本院权威名单）、时段偏好（上午/下午）；
  * LLM 路（qwen2.5:7b）：过敏史/慢性病/症状偏好 —— 输出必须过 JSON schema
    门控，解析失败/字段越界 → 整批丢弃（宁缺勿错）。

抽取用独立小 prompt，**与主对话隔离**；任何失败只记日志，绝不影响主流程。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# confidence 低于此值的事实直接丢弃
CONFIDENCE_FLOOR = 0.7

# 允许入库的事实 key（schema 门控：越界 key 整批丢弃）
ALLOWED_KEYS = frozenset(
    {"name", "id_card", "allergy", "chronic_disease", "preferred_department",
     "preferred_doctor", "preferred_time_slot", "symptom"}
)

# 身份证号（与 security/validators 的宽松捕获一致；入库前另过校验位）
_ID_CARD_RE = re.compile(r"\d{15,18}[0-9Xx]?")

# 显式姓名陈述（避免把任何 2-3 字词都当姓名）。
# 注意：规则跑在**原始用户文本**上（渲染标签"患者："不参与，
# 否则每条用户消息开头都会被当成"患者：X"姓名陈述）
_NAME_RE = re.compile(
    r"(?:我叫|我的名字是|患者姓名|患者名字|姓名)[：:]?\s*([\u4e00-\u9fa5]{2,4})"
)

# 时段偏好
_TIME_SLOT_RE = re.compile(r"(上午|下午|晚上)")

# LLM 抽取提示词（独立小 prompt，与主对话隔离）
_LLM_EXTRACT_PROMPT = """你是医疗信息抽取助手。从下面的医患对话中抽取患者的长期健康事实。
只抽取**确定性陈述**（患者明确说过的），不要推测。没有就输出空列表。

只允许这些 key：
- allergy（药物/食物过敏）
- chronic_disease（慢性病）
- symptom（长期症状）
- preferred_doctor（偏好的医生）

输出严格的 JSON（不要任何其他文字）：
{{"facts": [{{"key": "allergy", "value": "青霉素", "confidence": 0.95}}]}}

对话：
{dialogue}
"""


@dataclass
class FactEntry:
    """一条可持久化的患者事实"""

    key: str
    value: str
    source_trace_id: str = ""
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 1.0

    def is_valid(self) -> bool:
        return (
            self.key in ALLOWED_KEYS
            and bool(self.value.strip())
            and self.confidence >= CONFIDENCE_FLOOR
        )


# LLM 调用句柄：输入 prompt，返回模型输出文本（真实实现包 ChatModel.chat）
LlmFn = Callable[[str], Awaitable[str]]


class ProfileExtractor:
    """规则 + LLM 双路事实抽取（失败安全：任何异常都只记日志）。"""

    def __init__(
        self,
        llm_fn: Optional[LlmFn] = None,
        *,
        trace_id: str = "",
    ) -> None:
        self._llm_fn = llm_fn
        self._trace_id = trace_id

    async def extract(
        self,
        messages: Sequence[Any],
        *,
        authoritative_departments: Sequence[str] = (),
    ) -> List[FactEntry]:
        """从消息序列抽取事实（规则路 + LLM 路）。"""
        dialogue = self._render_dialogue(messages)
        raw_user_text = self._render_user_text(messages)
        if not raw_user_text.strip():
            return []

        facts: List[FactEntry] = []
        facts.extend(self._extract_rules(raw_user_text, authoritative_departments))
        facts.extend(await self._extract_llm(dialogue))
        # 门控：key 越界 / 空值 / 低置信度一律丢弃
        return [f for f in facts if f.is_valid()]

    # ---------- 规则路 ----------

    def _extract_rules(
        self, dialogue: str, departments: Sequence[str]
    ) -> List[FactEntry]:
        facts: List[FactEntry] = []

        for match in _ID_CARD_RE.finditer(dialogue):
            raw = match.group(0)
            # 只有用户消息里的号码才是患者的（渲染时已按角色标注，
            # 这里保守起见只收 18 位/15 位标准形态）
            if len(raw) in (15, 18):
                facts.append(FactEntry(key="id_card", value=raw.upper(), confidence=1.0))

        for match in _NAME_RE.finditer(dialogue):
            facts.append(
                FactEntry(key="name", value=match.group(1), confidence=0.95)
            )

        for match in _TIME_SLOT_RE.finditer(dialogue):
            facts.append(
                FactEntry(
                    key="preferred_time_slot", value=match.group(1), confidence=0.8
                )
            )

        # 科室：必须命中本院权威名单（避免把"内科"这种泛称当偏好存下来）
        for dept in departments:
            if dept and re.search(re.escape(dept), dialogue):
                facts.append(
                    FactEntry(
                        key="preferred_department", value=dept, confidence=0.85
                    )
                )
        return facts

    # ---------- LLM 路 ----------

    async def _extract_llm(self, dialogue: str) -> List[FactEntry]:
        """LLM 抽取，JSON schema 门控：任何解析失败 → 整批丢弃。"""
        if self._llm_fn is None:
            return []
        try:
            raw = await self._llm_fn(
                _LLM_EXTRACT_PROMPT.format(dialogue=dialogue[:3000])
            )
        except Exception as exc:  # 失败只记日志，不影响主流程
            logger.warning("画像 LLM 抽取失败（整批丢弃）：%s", exc)
            return []
        return self._parse_llm_facts(raw)

    def _parse_llm_facts(self, raw: str) -> List[FactEntry]:
        """解析并门控 LLM 输出；失败/越界 → 整批丢弃（宁缺勿错）。"""
        try:
            # 容忍模型包裹 ```json …```（门控的一部分：剥壳后再验）
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
            data = json.loads(text)
            items = data["facts"]
            if not isinstance(items, list):
                raise ValueError("facts 不是列表")
        except Exception as exc:
            logger.warning("画像 LLM 输出未通过 JSON 门控（整批丢弃）：%s", exc)
            return []

        facts: List[FactEntry] = []
        for item in items:
            try:
                entry = FactEntry(
                    key=str(item["key"]).strip(),
                    value=str(item["value"]).strip(),
                    confidence=float(item.get("confidence", 0.0)),
                    source_trace_id=self._trace_id,
                )
            except Exception as exc:
                # 单条畸形也整批丢弃（schema 门控：宁缺勿错）
                logger.warning("画像 LLM 输出含畸形条目（整批丢弃）：%s", exc)
                return []
            if entry.key not in ALLOWED_KEYS:
                logger.warning("画像 LLM 输出含越界 key=%s（整批丢弃）", entry.key)
                return []
            facts.append(entry)
        return facts

    # ---------- 工具 ----------

    @staticmethod
    def _render_dialogue(messages: Sequence[Any]) -> str:
        """把消息序列渲染成带角色标注的对话文本（给 LLM 用）。"""
        parts: List[str] = []
        for m in messages:
            content = str(getattr(m, "content", "") or "")
            if not content.strip():
                continue
            role = getattr(m, "role", None)
            role_value = getattr(role, "value", role)
            label = "患者" if role_value == "user" else "助手"
            parts.append(f"{label}：{content}")
        return "\n".join(parts)

    @staticmethod
    def _render_user_text(messages: Sequence[Any]) -> str:
        """只拼用户消息原文（规则路用，不带任何渲染标签）。"""
        parts: List[str] = []
        for m in messages:
            content = str(getattr(m, "content", "") or "")
            role = getattr(m, "role", None)
            role_value = getattr(role, "value", role)
            if role_value == "user" and content.strip():
                parts.append(content)
        return "\n".join(parts)
