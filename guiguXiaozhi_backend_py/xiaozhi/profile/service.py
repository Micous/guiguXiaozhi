"""画像服务编排 —— P2-M6。

把 M1 抽取器、M2 结构化存储、M3 向量库串起来：
  maybe_extract()  请求收口后调用（fire-and-forget）——
                   有身份证号才建画像（user_key = sha256(id_card)[:16]）；
                   每 profile_extract_interval_turns 轮新消息才抽一次；
                   抽取失败只记日志，绝不影响主对话。
  forget()         "忘记我"：删 MySQL 画像 + 向量事实。

调用方（application.py）只在 `profile_enabled=true` 时构造本服务；
开关关闭时 engine 收不到任何画像组件，行为与一期完全一致（红线）。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from xiaozhi.profile.extractor import ALLOWED_KEYS, ProfileExtractor
from xiaozhi.profile.store import FactCipher, ProfileRepository, derive_user_key
from xiaozhi.profile.vector import ProfileVectorStore

logger = logging.getLogger(__name__)


class ProfileService:
    def __init__(
        self,
        repository: ProfileRepository,
        cipher: FactCipher,
        llm_fn=None,
        vector_store: Optional[ProfileVectorStore] = None,
        *,
        extract_interval_turns: int = 6,
    ) -> None:
        self.repository = repository
        self.cipher = cipher
        self.vector_store = vector_store
        self.extract_interval_turns = max(1, extract_interval_turns)
        self._last_extracted_count: dict = {}  # user_key -> 已抽取过的消息数
        self._llm_fn = llm_fn

    def find_user_key(self, user_input: str, history: Sequence[Any] = ()) -> str:
        for text in [user_input] + [
            str(getattr(m, "content", "") or "") for m in reversed(list(history or []))
        ]:
            from xiaozhi.profile.extractor import _ID_CARD_RE

            match = _ID_CARD_RE.search(text or "")
            if match and len(match.group(0)) in (15, 18):
                return derive_user_key(match.group(0))
        return ""

    async def maybe_extract(
        self, user_input: str, messages: Sequence[Any], *, trace_id: str = ""
    ) -> int:
        """会话收口钩子：满足节奏就抽取并入库，返回入库条数。"""
        try:
            user_key = self.find_user_key(user_input, messages)
            if not user_key:
                return 0  # 无身份证号的会话不建画像（红线）

            total = len(messages)
            last = self._last_extracted_count.get(user_key, 0)
            if total - last < self.extract_interval_turns:
                return 0  # 未到抽取节奏

            extractor = ProfileExtractor(llm_fn=self._llm_fn, trace_id=trace_id)
            facts = await extractor.extract(list(messages))
            self._last_extracted_count[user_key] = total
            if not facts:
                return 0

            stored = 0
            for fact in facts:
                self.repository.upsert(user_key, fact, self.cipher)
                if self.vector_store is not None and fact.key in (
                    "allergy", "chronic_disease", "symptom",
                    "preferred_department", "preferred_doctor", "preferred_time_slot",
                ):
                    await self.vector_store.add(user_key, fact.key, fact.value)
                stored += 1
            logger.info(
                "画像更新：user_key=%s 新增 %s 条事实（%s 条候选）",
                user_key, stored, len(facts),
            )
            return stored
        except Exception as exc:  # 画像失败绝不影响主对话
            logger.warning("画像抽取失败（忽略）：%s", exc)
            return 0

    async def forget(self, user_key: str) -> int:
        """"忘记我"：删除 MySQL 画像 + 向量事实（合规要求，缺一不可）。"""
        removed = self.repository.delete(user_key)
        if self.vector_store is not None:
            try:
                await self.vector_store.delete_user(user_key)
            except Exception as exc:  # 向量清理失败不阻断，但要留痕
                logger.warning("画像向量删除失败：user_key=%s %s", user_key, exc)
        logger.info("画像已删除：user_key=%s（MySQL %s 条）", user_key, removed)
        return removed
