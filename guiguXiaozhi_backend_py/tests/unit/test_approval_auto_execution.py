"""审批兜底：用户确认后，**服务端拿着审批记录直接执行**（2026-11-12）。

真机事故（8081 + 5173 手工操作）：

    用户点「确认执行」→ 前端带一次性 token 重发
    轮1 模型：请在确认窗口核对上述信息并确认预约。未确认前不会进行实际挂号操作。
    轮1 引擎：追加强制执行指令（"用户已经完成确认，必须真正调用工具"）
    轮2 模型：已收到您的确认，正在为您提交预约，请稍候。
    → 两轮都零工具调用：token 白拿、预约**没有发生**，用户却被告知"正在提交"。

结论：这件事不该由模型决定。token 是用户亲手点下的确认凭据，参数是用户
在确认窗里核对过的那组（落在 ``ApprovalRecord.canonical_args``），执行器
只需要照做。本文件钉住三条底线：

  1. **真的执行了**（工具被调用一次、参数=审批记录里的那组、token 被消费）；
  2. **没有为兜底放宽任何安全边界**（重放/过期/跨会话/缺权限照样拒绝）；
  3. **如实告知**（成功说成功、失败说没办成、UNKNOWN 说核实中）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.tool import (  # noqa: E402
    RiskLevel,
    Tool,
    ToolDefinition,
    ToolParameter,
    ToolResult,
)
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.repositories.approval_repo import (  # noqa: E402
    ApprovalStatus,
    InMemoryApprovalRepository,
)
from xiaozhi.runtime.approval_execution import (  # noqa: E402
    APPROVED_DONE_DEFAULT,
    approved_operation_reply,
    strip_model_directed,
)
from xiaozhi.runtime.response_gate import UNKNOWN_NOTICE  # noqa: E402
from xiaozhi.security.approval_service import (  # noqa: E402
    ApprovalContext,
    ApprovalService,
    reset_approval_context,
    set_approval_context,
)
from xiaozhi.security.permissions import PermissionDecisionEngine, PermissionLevel  # noqa: E402
from xiaozhi.security.principal import DEFAULT_SCOPES, Principal  # noqa: E402
from xiaozhi.security.secure_executor import SecureToolExecutor  # noqa: E402

from test_engine_characterization import make_engine  # noqa: E402
from test_engine_reliability import FakeChatModel, FakeStore  # noqa: E402

ALICE = Principal(subject_id="alice", scopes=DEFAULT_SCOPES, auth_method="jwt")
SESSION = "sess-1"
TRUTH_ARGS = {
    "username": "王新勃",
    "id_card": "610121199401032831",
    "department": "呼吸内科",
    "date": "2026-11-14",
    "time": "上午",
}
MASKED_ARGS = dict(TRUTH_ARGS, id_card="610121********2831")

# 模型"只说不做"的原话（真机轮1）
MODEL_STALL_TEXT = "请在确认窗口核对上述信息并确认预约。未确认前不会进行实际挂号操作。"


class BookTool(Tool):
    """最小化的「预约挂号」写工具（记录被真正调用时的入参）。"""

    def __init__(self, result: ToolResult | None = None) -> None:
        self.call_count = 0
        self.received: dict | None = None
        self._result = result

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="预约挂号",
            description="提交预约",
            parameters=[
                ToolParameter("username", "string", "患者姓名"),
                ToolParameter("id_card", "string", "身份证号"),
                ToolParameter("department", "string", "科室"),
                ToolParameter("date", "string", "日期"),
                ToolParameter("time", "string", "时段"),
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
        if self._result is not None:
            return self._result
        return ToolResult.ok(
            f"预约成功。预约详情：姓名：{params['username']}，"
            f"身份证号：610121********2831，科室：{params['department']}，"
            f"日期：{params['date']}，时段：{params['time']}，医生：徐国兴"
        )


class SingleToolRegistry:
    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    def list_definitions(self):
        return []

    def get(self, name):
        return self._tool if name == "预约挂号" else None


@pytest.fixture(autouse=True)
def _clean_context():
    reset_approval_context()
    yield
    reset_approval_context()


def _build(tool: Tool, *, policy=PermissionLevel.AUTO):
    repo = InMemoryApprovalRepository()
    service = ApprovalService(repo, ttl_seconds=600.0)
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={"预约挂号": policy}),
        no_ui_policy="deny",
        approval_service=service,
    )
    engine, metrics = make_engine(
        None,  # 占位，下面替换
        registry=SingleToolRegistry(tool),
        executor=executor,
        store=FakeStore(),
    )
    return engine, metrics, service, repo, executor


def _approve(service: ApprovalService, args: dict | None = None):
    record = service.create(ALICE, SESSION, "预约挂号", dict(args or TRUTH_ARGS))
    token = service.approve(record.approval_id, ALICE)
    set_approval_context(
        ApprovalContext(principal=ALICE, approval_token=token, session_id=SESSION)
    )
    return record, token


async def _run(engine, message: str = "帮我预约呼吸内科，王新勃，610121199401032831，2026-11-14 上午"):
    parts, events = [], []
    async for event in engine.run(SESSION, message, user_id="alice"):
        events.append(event)
        if event.event_type == EventType.TEXT_RESPONSE:
            parts.append(event.metadata.get("delta", ""))
    return "".join(parts), events


# ---------- 1. 核心：带 token + 模型零工具调用 → 服务端真的执行了 ----------


async def test_approved_operation_is_executed_even_if_model_stalls():
    tool = BookTool()
    engine, metrics, service, repo, _executor = _build(tool)
    record, _token = _approve(service)
    model = FakeChatModel(
        [{"chunks": [MODEL_STALL_TEXT], "turn": ChatTurn(text=MODEL_STALL_TEXT)}]
    )
    engine.chat_model = model

    text, events = await _run(engine)

    # 真的执行了：工具被调用一次、参数=审批记录里的那组（真值）
    assert tool.call_count == 1
    assert tool.received == TRUTH_ARGS
    # token 被原子消费
    assert repo.get(record.approval_id).status == ApprovalStatus.CONSUMED
    # 用户看到的是真实结果，不是"请确认"
    assert "预约成功" in text
    assert MODEL_STALL_TEXT not in text
    # 不再"求模型配合"：只有 1 次模型调用（旧行为要追加一轮强制指令）
    assert model.calls == 1
    assert metrics.snapshot()["counters"]["approval_auto_executions"] == 1
    assert any(
        e.event_type == EventType.TOOL_EXECUTE
        and e.metadata.get("approval_auto_execution")
        for e in events
    )
    end = [e for e in events if e.event_type == EventType.AGENT_END][-1]
    assert end.metadata["approval_auto_executed"] is True


async def test_auto_execution_uses_approved_args_not_masked_ones():
    """模型手里只有掩码时也一样 —— 执行参数来自审批记录，与模型无关。"""
    tool = BookTool()
    engine, _metrics, service, repo, _executor = _build(tool)
    record, _token = _approve(service, MASKED_ARGS)  # 极端情况：记录里就是掩码
    model = FakeChatModel([{"chunks": ["好的。"], "turn": ChatTurn(text="好的。")}])
    engine.chat_model = model

    await _run(engine)

    assert tool.call_count == 1
    assert tool.received["id_card"] == "610121********2831"
    assert repo.get(record.approval_id).status == ApprovalStatus.CONSUMED


# ---------- 2. 安全边界一条都没放宽 ----------


async def test_no_token_keeps_model_answer_and_does_not_execute():
    tool = BookTool()
    engine, _metrics, _service, _repo, _executor = _build(tool)
    model = FakeChatModel([{"chunks": ["请问需要预约哪个科室？"], "turn": ChatTurn(text="请问需要预约哪个科室？")}])
    engine.chat_model = model

    text, _events = await _run(engine)

    assert tool.call_count == 0
    assert "请问需要预约哪个科室" in text


async def test_replayed_token_is_not_executed():
    """已消费的 token 重放 → 不执行（不能靠兜底绕过一次性消费）。"""
    tool = BookTool()
    engine, _metrics, service, repo, _executor = _build(tool)
    record, token = _approve(service)
    # 模拟：token 已经被上一次请求消费掉
    service.consume(
        token,
        ALICE,
        session_id=SESSION,
        tool_name="预约挂号",
        canonical_args=dict(TRUTH_ARGS),
    )
    assert repo.get(record.approval_id).status == ApprovalStatus.CONSUMED
    model = FakeChatModel([{"chunks": ["该预约已处理过。"], "turn": ChatTurn(text="该预约已处理过。")}])
    engine.chat_model = model

    text, _events = await _run(engine)

    assert tool.call_count == 0
    assert text


async def test_expired_token_is_not_executed():
    tool = BookTool()
    repo = InMemoryApprovalRepository()
    service = ApprovalService(repo, ttl_seconds=1.0)
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={"预约挂号": PermissionLevel.AUTO}),
        no_ui_policy="deny",
        approval_service=service,
    )
    record = service.create(ALICE, SESSION, "预约挂号", dict(TRUTH_ARGS))
    token = service.approve(record.approval_id, ALICE)
    set_approval_context(
        ApprovalContext(principal=ALICE, approval_token=token, session_id=SESSION)
    )
    # 手工把记录推到过期（等价于 TTL 到点）
    stored = repo.get(record.approval_id)
    stored.expires_at = stored.created_at
    repo.save(stored)

    engine, _metrics = make_engine(
        None, registry=SingleToolRegistry(tool), executor=executor, store=FakeStore()
    )
    engine.chat_model = FakeChatModel([{"chunks": ["审批已过期，请重新预约。"], "turn": ChatTurn(text="审批已过期，请重新预约。")}])

    text, _events = await _run(engine)

    assert tool.call_count == 0
    assert text


async def test_cross_subject_token_is_not_executed():
    """别人的 token 不能借兜底执行（主体不匹配 → ApprovalRejected）。"""
    tool = BookTool()
    engine, _metrics, service, _repo, _executor = _build(tool)
    record = service.create(ALICE, SESSION, "预约挂号", dict(TRUTH_ARGS))
    token = service.approve(record.approval_id, ALICE)
    bob = Principal(subject_id="bob", scopes=DEFAULT_SCOPES, auth_method="jwt")
    set_approval_context(
        ApprovalContext(principal=bob, approval_token=token, session_id=SESSION)
    )
    engine.chat_model = FakeChatModel([{"chunks": ["该操作需要本人确认。"], "turn": ChatTurn(text="该操作需要本人确认。")}])

    text, _events = await _run(engine)

    assert tool.call_count == 0
    assert text


async def test_missing_write_scope_is_not_executed():
    tool = BookTool()
    repo = InMemoryApprovalRepository()
    service = ApprovalService(repo, ttl_seconds=600.0)
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={"预约挂号": PermissionLevel.AUTO}),
        no_ui_policy="deny",
        approval_service=service,
    )
    record = service.create(ALICE, SESSION, "预约挂号", dict(TRUTH_ARGS))
    token = service.approve(record.approval_id, ALICE)
    no_scope = Principal(subject_id="alice", scopes=frozenset(), auth_method="jwt")
    set_approval_context(
        ApprovalContext(
            principal=no_scope,
            approval_token=token,
            session_id=SESSION,
            auth_enabled=True,
        )
    )
    engine, _metrics = make_engine(
        None, registry=SingleToolRegistry(tool), executor=executor, store=FakeStore()
    )
    engine.chat_model = FakeChatModel([{"chunks": ["当前账号缺少写权限。"], "turn": ChatTurn(text="当前账号缺少写权限。")}])

    text, _events = await _run(engine)

    # L0 的 scope 校验照样生效：兜底不是后门
    assert tool.call_count == 0


# ---------- 3. 如实告知（成败都不许包装） ----------


async def test_failed_execution_is_reported_honestly():
    tool = BookTool(
        ToolResult.fail(
            "参数校验未通过：本院没有【内科】。请根据提示修正参数后重新调用。",
            error_type="ValidationError",
        )
    )
    engine, _metrics, service, _repo, _executor = _build(tool)
    _approve(service)
    engine.chat_model = FakeChatModel(
        [{"chunks": [MODEL_STALL_TEXT], "turn": ChatTurn(text=MODEL_STALL_TEXT)}]
    )

    text, _events = await _run(engine)

    assert tool.call_count == 1
    assert "未能完成" in text
    assert "本院没有【内科】" in text
    # 面向模型的收尾指令不该念给用户
    assert "请根据提示" not in text
    assert "预约成功" not in text


async def test_unknown_result_uses_fixed_notice():
    tool = BookTool(
        ToolResult.fail(
            "结果核实中，请勿重复提交。",
            error_type="UnknownState",
            authoritative_status="UNKNOWN",
        )
    )
    engine, _metrics, service, _repo, _executor = _build(tool)
    _approve(service)
    engine.chat_model = FakeChatModel(
        [{"chunks": [MODEL_STALL_TEXT], "turn": ChatTurn(text=MODEL_STALL_TEXT)}]
    )

    text, events = await _run(engine)

    assert UNKNOWN_NOTICE in text
    # Response Gate 不会把同一句再下发一遍（界面上不重复）
    deltas = [e.metadata["delta"] for e in events if e.event_type == EventType.TEXT_RESPONSE]
    assert sum(1 for d in deltas if UNKNOWN_NOTICE in d) == 1


# ---------- 4. 落库三段式（下一轮模型看到的是真事实） ----------


async def test_history_is_three_segment_and_free_of_stall_text():
    tool = BookTool()
    engine, _metrics, service, _repo, _executor = _build(tool)
    _approve(service)
    store = FakeStore()
    engine.conversation_store = store
    engine.chat_model = FakeChatModel(
        [{"chunks": [MODEL_STALL_TEXT], "turn": ChatTurn(text=MODEL_STALL_TEXT)}]
    )

    await _run(engine)

    saved = store.saved
    assert [m.role.value for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert saved[1].tool_calls and saved[1].tool_calls[0].name == "预约挂号"
    # 叙述段留空：模型那句"请在确认窗口核对"绝不能进历史
    assert saved[1].content == ""
    # tool_call_id 必须配对，否则下一轮组装出的历史是残缺序列
    assert saved[2].tool_call_id == saved[1].tool_calls[0].id
    assert "预约成功" in saved[3].content
    # 落库的调用参数是掩码（PII 契约不变）
    assert saved[1].tool_calls[0].arguments["id_card"] == "610121********2831"


# ---------- 5. 回复文案（纯函数） ----------


def test_strip_model_directed_tail():
    assert (
        strip_model_directed("参数校验未通过：本院没有【内科】。请根据提示修正参数后重新调用。")
        == "参数校验未通过：本院没有【内科】。"
    )
    assert strip_model_directed("预约成功。") == "预约成功。"


def test_reply_success_keeps_tool_content():
    reply = approved_operation_reply(ToolResult.ok("预约成功。预约详情：科室：呼吸内科"))
    assert reply.startswith("预约成功")


def test_reply_empty_success_uses_default():
    assert approved_operation_reply(ToolResult.ok("")) == APPROVED_DONE_DEFAULT


def test_reply_failure_says_not_done():
    reply = approved_operation_reply(
        ToolResult.fail("号源已满，无法办理。", error_type="NoSlotAvailable")
    )
    assert "未能完成" in reply
    assert "号源已满" in reply


def test_reply_masks_id_card_from_tool_content():
    """工具结果里若漏出完整身份证号，最后一道口子也必须打码。"""
    reply = approved_operation_reply(
        ToolResult.ok("预约成功。身份证号：610121199401032831")
    )
    assert "610121199401032831" not in reply
