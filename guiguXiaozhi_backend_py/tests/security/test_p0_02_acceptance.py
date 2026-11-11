"""P0-02 七条验收的收口测试（引擎链路 + 注册期契约）。

分散在 test_approval_lifecycle / test_approval_executor / test_approval_api
里的用例已覆盖状态机与 HTTP 层；本文件补齐「引擎短路」与「注册期默认拒绝」
这两处此前没有端到端证据的缺口，并给出 7 条验收的显式映射。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import ToolCall  # noqa: E402
from xiaozhi.core.tool import RiskLevel, ToolDefinition, ToolParameter, ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.tools.registry import ToolRegistry  # noqa: E402
from xiaozhi.tools.appointment_tools import (  # noqa: E402
    BookAppointmentTool,
    CancelAppointmentTool,
    QueryDoctorsTool,
    QuerySlotTool,
)

from test_engine_reliability import (  # noqa: E402
    FakeChatModel,
    FakeStore,
    collect,
    make_engine,
)

APPROVAL_RESULT = ToolResult.fail(
    "工具【预约挂号】需要您核对信息并明确确认，尚未执行。",
    error_type="ApprovalRequired",
    approval={
        "decision": "REQUIRE_APPROVAL",
        "reason_code": "pending_confirmation",
        "detail": {
            "approval_id": "ap-engine-test",
            "tool_name": "预约挂号",
            "status": "PENDING",
            "args": {"id_card": "110101*******123X"},
            "expires_at": "2026-11-11T12:00:00",
        },
        "message": "该操作（预约挂号）需要您核对信息并明确确认后才会执行。",
    },
)


class _ApprovalExecutor:
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls = 0

    async def execute(self, call, tool):
        self.calls += 1
        return self.result


# ---------------------------------------------------------------- 验收 1（引擎短路）


async def test_engine_stops_after_approval_required_without_second_model_call():
    """写工具被拦下后绝不再把「需确认」结果喂给模型生成「已预约成功」。

    这是 P0-02 在引擎层最容易漏掉的一环：qwen2.5:7b 看到工具失败结果
    会自行编造「已为您确认并预约成功」。短路后模型只被调用一次。
    """
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    text="好的，我来帮您预约。",
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})],
                )
            },
            # 如果短路失效，第二轮会被消费并吐出这句幻觉承诺
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
        ]
    )
    executor = _ApprovalExecutor(APPROVAL_RESULT)
    store = FakeStore()
    engine, _ = make_engine(model, executor=executor, store=store)
    text, events = await collect(engine, "帮我预约神经内科")

    assert executor.calls == 1
    assert model.calls == 1
    assert "已为您预约成功" not in text
    assert "需要您核对信息并明确确认" in text
    assert any(e.event_type == EventType.APPROVAL_REQUIRED for e in events)
    # 三段式落库：assistant(tool_calls) → tool → assistant(最终回复)
    roles = [m.role.value for m in store.saved]
    assert roles[-3:] == ["assistant", "tool", "assistant"]
    assert store.saved[-1].content == APPROVAL_RESULT.metadata["approval"]["message"]


async def test_engine_keeps_legacy_behavior_when_no_approval_metadata():
    """普通工具失败不触发审批短路（灰度关闭时行为零变更）。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="查询是否有号源", arguments={})]
                )
            },
            {"chunks": ["查询完成"], "turn": ChatTurn(text="查询完成")},
        ]
    )
    executor = _ApprovalExecutor(ToolResult.fail("暂无号源"))
    engine, _ = make_engine(model, executor=executor)
    text, events = await collect(engine, "还有号吗")
    assert "查询完成" in text
    assert model.calls == 2
    assert not any(e.event_type == EventType.APPROVAL_REQUIRED for e in events)


# ---------------------------------------------------------------- 验收 6（注册期默认拒绝）


def _real_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(
        [
            BookAppointmentTool(None, None, object()),
            QuerySlotTool(None, object()),
            QueryDoctorsTool(object()),
            CancelAppointmentTool(None),
        ]
    )
    return registry


def test_real_tools_all_declare_valid_contracts():
    """审批开启前组合根会跑 validate_contracts；四个真实工具必须全部合规。"""
    registry = _real_registry()
    assert registry.validate_contracts() == []
    write_tools = [d.name for d in registry.list_definitions() if d.is_write]
    assert set(write_tools) == {"预约挂号", "取消预约挂号"}
    for name in write_tools:
        definition = registry.definition(name)
        assert definition.approval_required is True
        assert definition.idempotency_required is True


def test_unknown_risk_level_is_rejected_at_definition_time():
    # 第一道：__post_init__ 直接拒绝未知风险级别，不让它混进注册表
    with pytest.raises(ValueError, match="risk_level 未知"):
        ToolDefinition(
            name="未知风险工具",
            description="测试",
            parameters=[ToolParameter("x")],
            risk_level="high_write",  # type: ignore[arg-type]
        )

    # 第二道：即便有人绕过构造（反序列化/热更新）塞进非法值，
    # 注册期体检也必须报出来，让组合根 fail-fast 而不是运行时猜
    defn = ToolDefinition(name="未知风险工具", description="测试")
    defn.risk_level = "high_write"  # type: ignore[assignment]
    assert "未知风险级别" in (defn.validate_contract() or "")
    registry = ToolRegistry()

    class _BadTool:
        def definition(self):
            return defn

    registry.register(_BadTool())  # type: ignore[arg-type]
    assert any("未知风险级别" in p for p in registry.validate_contracts())


def test_write_tool_without_idempotency_is_rejected():
    defn = ToolDefinition(
        name="未声明幂等",
        description="测试",
        parameters=[ToolParameter("x")],
        risk_level=RiskLevel.WRITE,
    )
    assert "未声明幂等" in (defn.validate_contract() or "")


def test_unregistered_tool_is_denied_by_default_policy():
    """未登记工具在权限引擎里默认 DENY（验收 6 的另一半）。"""
    from xiaozhi.security.permissions import Decision, PermissionDecisionEngine

    engine = PermissionDecisionEngine()
    assert engine.decide("从未注册过的工具", "alice") == Decision.DENY
