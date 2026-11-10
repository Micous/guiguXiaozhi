"""画像注入器 —— P2-M4。

把 MySQL 精确事实 + 向量模糊事实合成一段 `[患者档案]` 文本，
由 engine 传给 ContextAssembler（注入 system prompt，预算 ≤150 token）。

措辞红线：只陈述事实，不指挥行为（沿用 RAG 教训——"请参考…回答"
这类措辞会压掉工具调用率）；operation_request 请求照常注入
（挂号时过敏史恰恰最有用）。

合规红线：注入文本**永不包含完整身份证号**（脱敏为 6101…2831）；
超过 180 天未被引用的事实不再注入。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from xiaozhi.profile.extractor import _ID_CARD_RE
from xiaozhi.profile.store import ProfileRepository, StoredFact
from xiaozhi.profile.vector import ProfileVectorStore

logger = logging.getLogger(__name__)

# 注入段 token 预算（与 memory_token_budget 的近似估算口径一致：~1.6 字/token）
PROFILE_TOKEN_BUDGET = 150
PROFILE_MAX_CHARS = 240

# 时效：超过 180 天未更新的事实不再注入
FACT_TTL_DAYS = 180

# 身份证脱敏：保留前 4 后 4
_MASK_ID_RE = re.compile(r"(\d{4})\d*(\d{4}[0-9Xx])")

_FACT_LABELS = {
    "allergy": "过敏史",
    "chronic_disease": "慢性病",
    "symptom": "既往症状",
    "preferred_department": "常挂科室",
    "preferred_doctor": "偏好医生",
    "preferred_time_slot": "偏好时段",
    "name": "姓名",
}


def mask_id_card(text: str) -> str:
    """任何文本里的完整身份证号 → 6101…2831 形态（红线：注入不带完整号码）。"""

    def _mask(m: "re.Match[str]") -> str:
        raw = m.group(0)
        if len(raw) < 12:
            return raw
        return f"{raw[:4]}…{raw[-4:]}"

    return _MASK_ID_RE.sub(_mask, text)


@dataclass
class ProfileInjector:
    """组装注入段（无状态）。任一依赖为 None 时能力降级为空段。"""

    repository: Optional[ProfileRepository] = None
    vector_store: Optional[ProfileVectorStore] = None

    def find_user_key(self, user_input: str, history: Sequence) -> str:
        """从当前输入+历史里找身份证号 → user_key（无身份证号不建画像）。"""
        for text in [user_input] + [
            str(getattr(m, "content", "") or "") for m in reversed(list(history or []))
        ]:
            match = _ID_CARD_RE.search(text or "")
            if match and len(match.group(0)) in (15, 18):
                from xiaozhi.profile.store import derive_user_key

                return derive_user_key(match.group(0))
        return ""

    async def build_segment(self, user_input: str, history: Sequence = ()) -> str:
        """合成注入段；任何失败都返回空串（画像只锦上添花，绝不添乱）。"""
        if self.repository is None:
            return ""
        try:
            user_key = self.find_user_key(user_input, history)
            if not user_key:
                return ""

            lines: List[str] = []
            # 精确事实（MySQL）：脱敏后陈述
            facts: List[StoredFact] = self.repository.get_all(user_key)
            now = datetime.now(timezone.utc)
            for fact in facts:
                if not self._is_fresh(fact, now):
                    continue
                label = _FACT_LABELS.get(fact.key, fact.key)
                value = mask_id_card(fact.value)
                if fact.key == "name":
                    # 姓名可直呼（不算敏感号码），但要脱敏其他可能混入的号码
                    lines.append(f"{value}（已实名）")
                else:
                    lines.append(f"{label}：{value}")

            # 模糊事实（向量）：当前对话语义相关的 top-3
            if self.vector_store is not None:
                hits = await self.vector_store.search(user_input, user_key, top_k=3)
                for hit in hits:
                    if hit.score < 0.3:
                        continue
                    snippet = mask_id_card(hit.text)
                    if snippet and snippet not in " ".join(lines):
                        lines.append(snippet)

            if not lines:
                return ""
            segment = "[患者档案] " + "；".join(lines) + "。"
            # 预算硬截断（超预算的画像宁缺勿滥）
            if len(segment) > PROFILE_MAX_CHARS:
                segment = segment[:PROFILE_MAX_CHARS].rstrip("；。,， ") + "。"
            return segment
        except Exception as exc:
            logger.warning("画像注入段组装失败（返回空）：%s", exc)
            return ""

    @staticmethod
    def _is_fresh(fact: StoredFact, now: datetime) -> bool:
        """超过 FACT_TTL_DAYS 且本次未引用的事实不再注入。"""
        if not fact.updated_at:
            return True
        try:
            ts = datetime.fromisoformat(str(fact.updated_at))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (now - ts).days
            return age_days <= FACT_TTL_DAYS
        except Exception:
            return True
