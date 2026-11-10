"""P2-M4/M5 测试：画像注入路径 + 三条合规红线。

M4（注入）：
  * 注入段脱敏、TTL、预算截断、无身份证号返回空；
  * engine 把 build_segment 的结果传给 ContextAssembler.assemble(profile=...)；
  * engine 落库成功后 fire-and-forget 调用 ProfileService.maybe_extract。

M5（红线，一条都不能破）：
  1. 画像不参与预约链路——appointment_tools / appointment_repo 源码零 profile 引用
     （find_duplicate 等重复判断只看预约表本身）；
  2. 注入文本永不包含完整身份证号（脱敏为 6101…2831 形态）；
  3. profile_enabled=false（即 engine 收到 None 组件）行为与一期完全一致。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet  # noqa: E402

from xiaozhi.core.message import Message, Role  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.profile.extractor import FactEntry  # noqa: E402
from xiaozhi.profile.injector import PROFILE_MAX_CHARS, ProfileInjector, mask_id_card  # noqa: E402
from xiaozhi.profile.service import ProfileService  # noqa: E402
from xiaozhi.profile.store import (  # noqa: E402
    FactCipher,
    InMemoryProfileRepository,
    StoredFact,
    derive_user_key,
)
from xiaozhi.profile.vector import InMemoryProfileVectorStore  # noqa: E402
from xiaozhi.reliability.resilience import RetryConfig  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine  # noqa: E402

from test_engine_reliability import (  # noqa: E402
    FakeAssembler,
    FakeChatModel,
    FakeExecutor,
    FakeRegistry,
    FakeStore,
    collect,
)

SECRET = Fernet.generate_key().decode()
ID_CARD = "610121199401032831"  # 过 GB 11643 校验位的测试号
USER_KEY = derive_user_key(ID_CARD)


def make_cipher_repo():
    return FactCipher(SECRET), InMemoryProfileRepository()


# ---------- M4：注入段组装 ----------


def test_mask_id_card_18_and_15():
    masked = mask_id_card("610121199401032831")
    assert masked == "6101…2831"
    assert "610121199401032831" not in masked
    # 15 位号也要脱敏
    short = mask_id_card("110105491001001")
    assert short.startswith("1101") and short.endswith("0001") or "…" in short
    assert "110105491001001" not in short


async def test_segment_contains_facts_and_masks_sensitive():
    cipher, repo = make_cipher_repo()
    repo.upsert(USER_KEY, FactEntry(key="allergy", value="青霉素", confidence=0.9), cipher)
    repo.upsert(
        USER_KEY,
        FactEntry(key="id_card", value=ID_CARD, confidence=0.95),
        cipher,
    )
    injector = ProfileInjector(repository=repo)
    segment = await injector.build_segment(f"我是{ID_CARD}，帮我预约", [])
    assert "过敏史：青霉素" in segment
    assert ID_CARD not in segment  # 红线：完整号码不进上下文


async def test_ttl_stale_fact_not_injected():
    cipher, repo = make_cipher_repo()
    stale = StoredFact(
        key="allergy",
        value="青霉素",
        updated_at=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
    )
    fresh = StoredFact(
        key="chronic_disease",
        value="高血压",
        updated_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
    )
    repo._data[USER_KEY] = {"allergy": stale, "chronic_disease": fresh}
    injector = ProfileInjector(repository=repo)
    segment = await injector.build_segment(ID_CARD, [])
    assert "高血压" in segment
    assert "青霉素" not in segment  # 180 天 TTL：过期事实不再注入


async def test_budget_hard_truncation():
    cipher, repo = make_cipher_repo()
    for i in range(20):
        repo.upsert(
            USER_KEY,
            FactEntry(key="symptom", value=f"症状{i}：" + "很长的描述" * 10, confidence=0.9),
            cipher,
        )
    injector = ProfileInjector(repository=repo)
    segment = await injector.build_segment(ID_CARD, [])
    assert len(segment) <= PROFILE_MAX_CHARS + 1  # 截断 + 句号，宁缺勿滥


async def test_no_user_key_returns_empty():
    cipher, repo = make_cipher_repo()
    repo.upsert(USER_KEY, FactEntry(key="allergy", value="青霉素", confidence=0.9), cipher)
    injector = ProfileInjector(repository=repo)
    # 输入与历史都没有身份证号 → 不注入任何画像（红线：无号码不建/不用画像）
    assert await injector.build_segment("帮我预约神经内科", []) == ""


async def test_repository_none_degrades_to_empty():
    injector = ProfileInjector(repository=None)
    assert await injector.build_segment(ID_CARD, []) == ""


# ---------- M4：engine 接线 ----------


class SpyAssembler(FakeAssembler):
    def __init__(self):
        self.kwargs_seen = []

    def assemble(self, *args, **kwargs):
        self.kwargs_seen.append(kwargs)
        return super().assemble(*args, **kwargs)


class FakeInjector:
    async def build_segment(self, user_input, history):
        return "[患者档案] 过敏史：青霉素。"


class RecordingService:
    def __init__(self):
        self.calls = []

    async def maybe_extract(self, user_input, messages, *, trace_id=""):
        self.calls.append((user_input, list(messages), trace_id))
        return 0


def _make_engine(model, *, assembler=None, injector=None, service=None):
    return AgentEngine(
        chat_model=model,
        tool_registry=FakeRegistry(),
        secure_executor=FakeExecutor(),
        conversation_store=FakeStore(),
        context_assembler=assembler or FakeAssembler(),
        system_prompt="系统提示",
        metrics=MetricsCollector(),
        retry_config=RetryConfig(max_attempts=1),
        profile_injector=injector,
        profile_service=service,
    )


@pytest.mark.asyncio
async def test_engine_passes_profile_segment_to_assembler():
    assembler = SpyAssembler()
    model = FakeChatModel([{"chunks": ["好的。"], "turn": ChatTurn(text="好的。")}])
    engine = _make_engine(model, assembler=assembler, injector=FakeInjector())
    await collect(engine, ID_CARD)
    assert any(kw.get("profile") == "[患者档案] 过敏史：青霉素。" for kw in assembler.kwargs_seen)


@pytest.mark.asyncio
async def test_engine_without_injector_passes_empty_profile():
    """红线 3 的一半：组件为 None 时 assemble 的 profile 恒为空串。"""
    assembler = SpyAssembler()
    model = FakeChatModel([{"chunks": ["好的。"], "turn": ChatTurn(text="好的。")}])
    engine = _make_engine(model, assembler=assembler)
    assert engine.profile_injector is None and engine.profile_service is None
    await collect(engine, "帮我预约神经内科")
    assert all(kw.get("profile", "") == "" for kw in assembler.kwargs_seen)


@pytest.mark.asyncio
async def test_engine_fires_profile_extract_after_save():
    service = RecordingService()
    store = FakeStore()
    model = FakeChatModel([{"chunks": ["好的。"], "turn": ChatTurn(text="好的。")}])
    engine = AgentEngine(
        chat_model=model,
        tool_registry=FakeRegistry(),
        secure_executor=FakeExecutor(),
        conversation_store=store,
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=MetricsCollector(),
        retry_config=RetryConfig(max_attempts=1),
        profile_service=service,
    )
    await collect(engine, ID_CARD)
    # fire-and-forget：让事件循环把后台任务跑完
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending)
    assert len(service.calls) == 1
    user_input, messages, _trace = service.calls[0]
    assert user_input == ID_CARD
    assert store.saved == messages  # 抽取的是落库后的同一批消息


# ---------- M5：三条合规红线 ----------


def test_redline_profile_not_referenced_by_appointment_chain():
    """红线 1：画像绝不参与预约链路（find_duplicate 只看预约表本身）。

    源码级断言：预约工具与预约仓储的源码里不允许出现 profile 依赖。
    """
    base = ROOT / "xiaozhi"
    targets = [
        base / "tools" / "appointment_tools.py",
        base / "repositories" / "appointment_repo.py",
    ]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        assert "profile" not in src.lower(), f"{path.name} 引用了 profile——画像泄漏进预约链路"


@pytest.mark.asyncio
async def test_redline_injection_never_carries_full_id_card():
    """红线 2：不管事实值里混了什么，注入文本永不出现完整身份证号。"""
    cipher, repo = make_cipher_repo()
    for value in [
        ID_CARD,
        f"身份证号{ID_CARD}，请核对",
        "110105491001001",
        f"工号 12345，证件 {ID_CARD}",
    ]:
        repo.upsert(USER_KEY, FactEntry(key="symptom", value=value, confidence=0.9), cipher)
    injector = ProfileInjector(repository=repo)
    segment = await injector.build_segment(ID_CARD, [])
    assert ID_CARD not in segment
    assert "110105491001001" not in segment
    assert "6101…2831" in segment  # 只以脱敏形态出现


@pytest.mark.asyncio
async def test_redline_disabled_switch_defaults_match_phase_one():
    """红线 3：默认配置（profile_enabled=false）下组件为 None，注入与抽取全关。"""
    from xiaozhi.config import Settings

    settings = Settings()
    assert settings.profile_enabled is False
    injector = ProfileInjector(repository=None)
    assert await injector.build_segment("任意输入", []) == ""
    # 无向量库时 forget 只删 MySQL，不抛异常
    cipher, repo = make_cipher_repo()
    service = ProfileService(repository=repo, cipher=cipher)
    assert await service.forget(USER_KEY) == 0
