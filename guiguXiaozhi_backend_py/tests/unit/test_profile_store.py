"""画像存储单测 —— P2-M2（结构化存储 + 加密）与 M3（向量库隔离）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402

from xiaozhi.profile.extractor import FactEntry  # noqa: E402
from xiaozhi.profile.store import (  # noqa: E402
    FactCipher,
    InMemoryProfileRepository,
    ProfileDisabledError,
    SENSITIVE_KEYS,
    derive_user_key,
)
from xiaozhi.profile.vector import (  # noqa: E402
    PROFILE_COLLECTION,
    PROFILE_MILVUS_ALIAS,
    InMemoryProfileVectorStore,
)

SECRET = Fernet.generate_key().decode()


# ---------- M2：user_key 派生 ----------


def test_user_key_derivation_stable_and_anonymous():
    k1 = derive_user_key("610121199401032831")
    k2 = derive_user_key(" 610121199401032831 ")  # 前后空白不影响
    k3 = derive_user_key("610121199401032832")
    assert k1 == k2
    assert len(k1) == 16
    assert k1 != k3
    assert "610121" not in k1  # 派生值不泄漏身份证片段


# ---------- M2：覆盖式更新（覆盖即遗忘） ----------


def test_upsert_overwrites_old_fact():
    repo = InMemoryProfileRepository()
    repo.upsert("u1", FactEntry(key="allergy", value="青霉素", confidence=0.95))
    repo.upsert("u1", FactEntry(key="allergy", value="头孢", confidence=0.9))

    facts = {f.key: f.value for f in repo.get_all("u1")}
    assert facts["allergy"] == "头孢"  # 新事实顶旧事实


def test_delete_forgets_everything():
    repo = InMemoryProfileRepository()
    repo.upsert("u1", FactEntry(key="allergy", value="青霉素"))
    repo.upsert("u1", FactEntry(key="name", value="王新勃"))
    assert repo.delete("u1") == 2
    assert repo.get_all("u1") == []


# ---------- M2：敏感字段密文落库 ----------


def test_sensitive_value_encrypted_at_rest():
    cipher = FactCipher(SECRET)
    repo = InMemoryProfileRepository()
    repo.upsert("u1", FactEntry(key="id_card", value="610121199401032831"), cipher)
    repo.upsert("u1", FactEntry(key="allergy", value="青霉素"), cipher)

    # 落库原始值：敏感字段是密文，普通字段明文
    raw_id = repo._data["u1"]["id_card"].value
    raw_allergy = repo._data["u1"]["allergy"].value
    assert raw_id != "610121199401032831"
    assert "610121" not in raw_id
    assert raw_allergy == "青霉素"

    # 读出解密为明文
    got = repo.get("u1", "id_card", cipher)
    assert got is not None and got.value == "610121199401032831"


def test_sensitive_key_constant():
    assert "id_card" in SENSITIVE_KEYS
    assert "allergy" not in SENSITIVE_KEYS


def test_missing_secret_key_disables_module():
    """密钥缺失 → ProfileDisabledError（装配层据此整体禁用画像模块）。"""
    with pytest.raises(ProfileDisabledError):
        FactCipher("")
    # 无 cipher 时拒绝存敏感字段（合规兜底）
    repo = InMemoryProfileRepository()
    with pytest.raises(ProfileDisabledError):
        repo.upsert("u1", FactEntry(key="id_card", value="610121199401032831"))


# ---------- M3：向量库 ----------


def test_profile_vector_constants_isolated_from_knowledge():
    """集合/alias 与知识库隔离 —— 铁律写进常量并被测试钉住。"""
    assert PROFILE_MILVUS_ALIAS == "xiaozhi_profile_py"
    assert PROFILE_MILVUS_ALIAS != "xiaozhi_knowledge_py"
    assert PROFILE_COLLECTION == "xiaozhi_profile_facts"


def _fake_embed(dim=8):
    async def embed(text):
        # 确定性伪向量：按字符哈希铺满 dim
        v = [0.0] * dim
        for i, ch in enumerate(text):
            v[i % dim] += (ord(ch) % 7) / 7.0
        return v
    return embed


@pytest.mark.asyncio
async def test_vector_search_filters_by_user_key():
    """检索绝不出此用户的画像（user_key 标量过滤）。"""
    store = InMemoryProfileVectorStore(_fake_embed())
    await store.add("u1", "allergy", "对青霉素过敏，幼时服药起疹")
    await store.add("u2", "chronic_disease", "高血压十年")

    hits = await store.search("青霉素过敏史", "u1", top_k=3)
    assert hits, "应命中 u1 自己的事实"
    assert all(h.user_key == "u1" for h in hits)
    assert all("高血压" not in h.text for h in hits)


@pytest.mark.asyncio
async def test_vector_search_empty_store_returns_empty():
    store = InMemoryProfileVectorStore(_fake_embed())
    assert await store.search("任何问题", "u1") == []


@pytest.mark.asyncio
async def test_vector_add_empty_text_is_noop():
    store = InMemoryProfileVectorStore(_fake_embed())
    await store.add("u1", "allergy", "  ")
    assert await store.search("x", "u1") == []
