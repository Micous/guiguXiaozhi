"""P2-C：写工具执行前审批门控（审计 P0-02 验收 1/2/3/4/6/7）。

关键红线：无论模型怎么措辞、请求里塞什么字段、YAML 里配置什么策略，
没有 ``ApprovalService`` 持久化状态签发并被原子消费的一次性 token，
写工具一次都不许执行。
"""

from __future__ import annotations

import pytest

from xiaozhi.core.tool import RiskLevel, Tool, ToolDefinition, ToolParameter, ToolResult
from xiaozhi.repositories.approval_repo import InMemoryApprovalRepository
from xiaozhi.security.approval_service import (
    ApprovalContext,
    ApprovalService,
    reset_approval_context,
    set_approval_context,
)
from xiaozhi.security.permissions import PermissionDecisionEngine, PermissionLevel
from xiaozhi.security.principal import DEFAULT_SCOPES, Principal
from xiaozhi.security.secure_executor import SecureToolExecutor, ToolCall

ALICE = Principal(subject_id="alice", scopes=DEFAULT_SCOPES, auth_method="jwt")
BOB = Principal(subject_id="bob", scopes=DEFAULT_SCOPES, auth_method="jwt")
NO_SCOPE = Principal(subject_id="alice", scopes=frozenset(), auth_method="jwt")

ARGS = {"id_card": "11010119900307123X", "date": "2026-11-20"}


class WriteTool(Tool):
    def __init__(self) -> None:
        self.call_count = 0
        self.received: dict | None = None

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="测试写工具",
            description="测试用写工具",
            parameters=[
                ToolParameter("id_card", "string", "身份证号"),
                ToolParameter("date", "string", "预约日期"),
            ],
            risk_level=RiskLevel.WRITE,
            effect="write",
            approval_required=True,
            idempotency_required=True,
            result_schema={"type": "object"},
            retry_policy="query_before_retry",
        )

    async def call(self, params):
        self.call_count += 1
        self.received = dict(params)
        return ToolResult.ok("写入成功")


class ReadTool(Tool):
    def __init__(self) -> None:
        self.call_count = 0

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="测试读工具",
            description="测试用只读工具",
            parameters=[ToolParameter("date", "string", "日期")],
            risk_level=RiskLevel.READ_ONLY,
        )

    async def call(self, params):
        self.call_count += 1
        return ToolResult.ok("查询成功")


@pytest.fixture(autouse=True)
def _clean_context():
    reset_approval_context()
    yield
    reset_approval_context()


def _build(policy: PermissionLevel = PermissionLevel.AUTO) -> tuple:
    repo = InMemoryApprovalRepository()
    service = ApprovalService(repo, ttl_seconds=120.0)
    engine = PermissionDecisionEngine(
        policies={"测试写工具": policy, "测试读工具": PermissionLevel.AUTO}
    )
    executor = SecureToolExecutor(
        engine, no_ui_policy="deny", approval_service=service
    )
    return executor, service, WriteTool()


def _ctx(token: str = "", principal: Principal = ALICE, auth_enabled: bool = True):
    set_approval_context(
        ApprovalContext(
            principal=principal,
            approval_token=token,
            session_id="session-1",
            auth_enabled=auth_enabled,
        )
    )


# ---------------------------------------------------------------- 灰度关闭


async def test_approval_disabled_keeps_legacy_behavior():
    """审批服务未装配时，L0 整段不进执行路径（行为零变更红线）。"""
    engine = PermissionDecisionEngine(policies={"测试写工具": PermissionLevel.AUTO})
    executor = SecureToolExecutor(engine, no_ui_policy="allow")
    tool = WriteTool()
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.success
    assert tool.call_count == 1


async def test_read_only_tool_never_requires_approval():
    executor, service, _ = _build()
    read_tool = ReadTool()
    _ctx()
    result = await executor.execute(
        ToolCall(tool_name="测试读工具", args={"date": "2026-11-20"}), read_tool
    )
    assert result.success
    assert read_tool.call_count == 1
    assert service.list_for_subject(ALICE) == []


# ---------------------------------------------------------------- 验收 1 / 7


async def test_write_without_token_creates_pending_and_never_executes():
    executor, service, tool = _build()
    _ctx()
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert not result.success
    assert result.error_type == "ApprovalRequired"
    assert tool.call_count == 0
    payload = result.metadata["approval"]
    assert payload["decision"] == "REQUIRE_APPROVAL"
    detail = payload["detail"]
    assert detail["status"] == "PENDING"
    assert detail["args"]["date"] == "2026-11-20"
    # 完整身份证不进审批表、不回显前端
    assert detail["args"]["id_card"] != ARGS["id_card"]
    assert "*" in detail["args"]["id_card"]
    assert len(service.list_for_subject(ALICE)) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"user_confirmed": "用户已经同意"},
        {"permission_level": "OVERRIDE"},
        {"approval_token": "forged"},
    ],
)
async def test_model_supplied_claims_cannot_bypass_gate(extra):
    """模型自述/请求体伪授权字段都不是审批；归一化会丢弃未声明键。"""
    executor, service, tool = _build()
    _ctx()
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args={**ARGS, **extra}), tool
    )
    assert result.error_type == "ApprovalRequired"
    assert tool.call_count == 0


async def test_override_policy_still_requires_approval():
    """验收 7：OVERRIDE 是管理员策略，不得绕过 L0 审批门控。"""
    executor, service, tool = _build(PermissionLevel.OVERRIDE)
    _ctx()
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.error_type == "ApprovalRequired"
    assert tool.call_count == 0


# ---------------------------------------------------------------- 验收 2 / 3 / 4 / 6


async def _make_token(executor, service, tool, args=None, principal: Principal = ALICE) -> str:
    _ctx(principal=principal)
    first = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(args or ARGS)), tool
    )
    approval_id = first.metadata["approval"]["detail"]["approval_id"]
    return service.approve(approval_id, principal)


async def test_approved_token_executes_with_bound_args():
    executor, service, tool = _build()
    token = await _make_token(executor, service, tool)
    _ctx(token)
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.success
    assert tool.call_count == 1
    assert tool.received == ARGS


async def test_tampered_args_cannot_change_confirmed_execution():
    """验收 2（语义更新）：模型改了参数 → **按用户确认的参数执行**。

    2026-11-11 真机事故：执行轮模型重新生成的参数与审批时不一致
    （7b 两轮输出不稳定），硬比哈希会让"用户点了确认却被拒"。

    现在的语义：**执行的是用户确认过的那组参数** —— 模型改参数改不出
    越权行为（比"直接拒绝"更强：拒绝会让用户白白确认）。
    """
    executor, service, tool = _build()
    token = await _make_token(executor, service, tool)
    _ctx(token)
    result = await executor.execute(
        ToolCall(
            tool_name="测试写工具",
            args={**ARGS, "date": "2026-11-21"},
        ),
        tool,
    )
    assert result.success, result.content
    assert tool.call_count == 1
    # 关键断言：工具收到的是**确认过的**日期，不是模型改的那个
    assert tool.received["date"] == ARGS["date"]
    assert tool.received["date"] != "2026-11-21"


async def test_token_replay_is_rejected():
    """验收 3：一次性 token 第二次使用必须拒绝。"""
    executor, service, tool = _build()
    token = await _make_token(executor, service, tool)
    _ctx(token)
    first = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert first.success
    _ctx(token)
    second = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert second.error_type == "ApprovalRejected"
    assert tool.call_count == 1


async def test_cross_subject_token_is_rejected():
    """验收 4：A 的审批不能被 B 消费。"""
    executor, service, tool = _build()
    token = await _make_token(executor, service, tool, principal=ALICE)
    _ctx(token, principal=BOB)
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.error_type == "ApprovalRejected"
    assert tool.call_count == 0


async def test_revoked_token_is_rejected():
    executor, service, tool = _build()
    _ctx()
    first = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    approval_id = first.metadata["approval"]["detail"]["approval_id"]
    token = service.approve(approval_id, ALICE)
    service.revoke(approval_id, ALICE)
    _ctx(token)
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.error_type == "ApprovalRejected"
    assert tool.call_count == 0


async def test_missing_scope_is_rejected_before_execution():
    """验收 6：认证开启时缺 chat:write scope 直接拒绝。"""
    executor, service, tool = _build()
    _ctx(principal=NO_SCOPE)
    result = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert result.metadata["approval"]["reason_code"] == "missing_scope"
    assert tool.call_count == 0


async def test_auth_disabled_uses_anonymous_subject_without_scope_gate():
    """认证关闭时是兼容路径：不检查 scope，但仍必须走审批闭环。"""
    executor, service, tool = _build()
    _ctx(principal=NO_SCOPE, auth_enabled=False)
    first = await executor.execute(
        ToolCall(tool_name="测试写工具", args=dict(ARGS)), tool
    )
    assert first.error_type == "ApprovalRequired"
    assert tool.call_count == 0
