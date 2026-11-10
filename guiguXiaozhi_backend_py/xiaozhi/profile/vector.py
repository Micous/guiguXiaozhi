"""患者画像向量记忆 —— P2-M3。

Milvus 集合 `xiaozhi_profile_facts`，**独立 alias `xiaozhi_profile_py`** ——
与知识库 `xiaozhi_knowledge_py` 绝不混用（隔离是本项目铁律）。

只存**模糊描述类**事实（"对青霉素过敏，幼时服药起疹"、"偏好上午复诊"），
embed 后带 user_key 标量过滤；精确字段（身份证/姓名）只走 MySQL（store.py）——
精确的事精确查，模糊的事向量找。
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 与知识库隔离的独立 alias / 集合名（铁律，写成常量供测试钉住）
PROFILE_MILVUS_ALIAS = "xiaozhi_profile_py"
PROFILE_COLLECTION = "xiaozhi_profile_facts"

# 只进向量库的模糊事实 key（精确字段 id_card/name 只走 MySQL）
FUZZY_FACT_KEYS = frozenset(
    {"allergy", "chronic_disease", "symptom", "preferred_department",
     "preferred_doctor", "preferred_time_slot"}
)

EmbedFn = Callable[[str], "object"]  # async (text) -> List[float]


@dataclass
class ProfileHit:
    """一次向量画像检索命中"""

    text: str
    score: float
    user_key: str
    fact_key: str = ""


class ProfileVectorStore(ABC):
    """画像向量库接口"""

    @abstractmethod
    async def add(self, user_key: str, fact_key: str, text: str) -> None:
        """写入一条模糊事实（embed 后带 user_key 标量）"""

    @abstractmethod
    async def search(self, query: str, user_key: str, top_k: int = 3) -> List[ProfileHit]:
        """当前对话语义 top-N + user_key 过滤（绝不出此用户的画像）"""


class InMemoryProfileVectorStore(ProfileVectorStore):
    """内存实现（测试 / Milvus 不可用时的降级兜底）。余弦相似度。"""

    def __init__(self, embed_fn: EmbedFn) -> None:
        self._embed_fn = embed_fn
        self._vectors: List[Dict[str, object]] = []  # {user_key, fact_key, text, vector}
        self._lock = threading.Lock()

    async def add(self, user_key: str, fact_key: str, text: str) -> None:
        if not text.strip():
            return
        vector = await self._embed_fn(text)
        with self._lock:
            self._vectors.append(
                {"user_key": user_key, "fact_key": fact_key, "text": text, "vector": vector}
            )

    async def search(self, query: str, user_key: str, top_k: int = 3) -> List[ProfileHit]:
        if not self._vectors:
            return []
        qv = await self._embed_fn(query)
        scored: List[ProfileHit] = []
        with self._lock:
            candidates = [v for v in self._vectors if v["user_key"] == user_key]
        for item in candidates:
            score = _cosine(qv, item["vector"])  # type: ignore[arg-type]
            scored.append(
                ProfileHit(
                    text=str(item["text"]),
                    score=score,
                    user_key=user_key,
                    fact_key=str(item["fact_key"]),
                )
            )
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]


class MilvusProfileVectorStore(ProfileVectorStore):
    """Milvus 实现（独立 alias，启动时确保集合存在）"""

    def __init__(
        self,
        milvus_uri: str,
        embed_fn: EmbedFn,
        dimension: int,
        *,
        alias: str = PROFILE_MILVUS_ALIAS,
        collection: str = PROFILE_COLLECTION,
    ) -> None:
        from pymilvus import MilvusClient, DataType

        self._embed_fn = embed_fn
        self._dimension = dimension
        self._collection = collection
        self._client = MilvusClient(uri=milvus_uri, alias=alias)
        self._alias = alias
        if not self._client.has_collection(collection):
            schema = self._client.create_schema(auto_id=True, enable_dynamic_field=False)
            schema.add_field("id", DataType.INT64, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
            schema.add_field("user_key", DataType.VARCHAR, max_length=16)
            schema.add_field("fact_key", DataType.VARCHAR, max_length=32)
            schema.add_field("text", DataType.VARCHAR, max_length=1024)
            index_params = self._client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
            self._client.create_collection(collection, schema=schema, index_params=index_params)
            logger.info("画像向量集合已创建：%s（alias=%s）", collection, alias)

    async def add(self, user_key: str, fact_key: str, text: str) -> None:
        if not text.strip():
            return
        vector = await self._embed_fn(text)
        self._client.insert(
            self._collection,
            {"vector": vector, "user_key": user_key, "fact_key": fact_key, "text": text[:1024]},
        )

    async def search(self, query: str, user_key: str, top_k: int = 3) -> List[ProfileHit]:
        try:
            qv = await self._embed_fn(query)
            rows = self._client.search(
                self._collection,
                data=[qv],
                limit=top_k,
                filter=f'user_key == "{user_key}"',
                output_fields=["text", "fact_key", "user_key"],
            )
        except Exception as exc:
            logger.warning("画像向量检索失败（返回空）：%s", exc)
            return []
        hits: List[ProfileHit] = []
        for row in (rows[0] if rows else []):
            entity = row.get("entity", {})
            hits.append(
                ProfileHit(
                    text=entity.get("text", ""),
                    score=float(row.get("distance", 0.0)),
                    user_key=entity.get("user_key", user_key),
                    fact_key=entity.get("fact_key", ""),
                )
            )
        return hits


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
