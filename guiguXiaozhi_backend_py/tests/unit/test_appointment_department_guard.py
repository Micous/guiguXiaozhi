"""预约科室确定性守卫 / 审批前预校验 / 纯应承前缀 —— 护栏单测。

回归背景（2026-11-12 真机，trace 01de88f0 + f87ff2b6）：
用户填单「王新勃，610121199401032831，内科，2026.11.14」，本院没有"内科"。
模型先调了「查询是否有号源」，工具明确回了「本院没有【内科】这个科室…
等用户确认后再查询」，7B 却完全无视 ToolResult，继续要时段、承诺
"我将为您预约"；补「上午」后确认窗弹出，带 token 重发那轮更是
4 次 llm.chat / 43 秒 / 3 次虚假承诺截断 / 2 次纠正重试，
最后才把拒绝转述出来。

三层修复，本文件逐层钉住：
  1. 引擎前置短路：填单阶段就收尾，model.calls == 0、话术落库、不进审批；
  2. 审批门控前预校验：即使短路漏了，也不为不存在的科室生成 PENDING；
  3. 流内截断时纯应承前缀不下发，不再单独刷一个「好的，好的，好的，」气泡。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
from xiaozhi.runtime.appointment_guard import assess_department_guard  # noqa: E402
from xiaozhi.runtime.stream_filter import is_pure_filler  # noqa: E402
from xiaozhi.security.medical_safety import EMERGENCY_REPLY  # noqa: E402
from xiaozhi.security.output_guard import scan_medical_output  # noqa: E402
from xiaozhi.security.permissions import PermissionDecisionEngine  # noqa: E402
from xiaozhi.security.secure_executor import SecureToolExecutor, ToolCall  # noqa: E402
from xiaozhi.tools.appointment_tools import BookAppointmentTool  # noqa: E402

from test_engine_reliability import (  # noqa: E402
    FakeChatModel,
    FakeStore,
    collect,
    make_engine,
)

KNOWN = (
    "儿科", "内分泌科", "呼吸内科", "妇产科", "康复医学科", "心内科", "消化内科",
    "皮肤科", "眼科", "神经内科", "神经外科", "耳鼻咽喉头颈外科", "骨科",
)
FORM = "王新勃，610121199401032831，内科，2026.11.14"


class FakeDoctorService:
    def __init__(self, departments=KNOWN, fail=False):
        self._departments = list(departments)
        self._fail = fail

    def list_departments(self):
        if self._fail:
            raise RuntimeError("HIS 主档不可用")
        return list(self._departments)

    def list_doctors(self, department=""):
        return []


# ---------------------------------------------------------------- 纯判定


@pytest.mark.parametrize(
    "text",
    [
        FORM,                                             # 真机原始填单
        "王新勃，610121199401032831，内科，2026-11-14",   # 横杠日期
        "帮我挂内科的号",                                 # 自由文本
        "我要预约心脏外科",                               # 名单外科室
        "预约挂号，内科，上午",                           # 带动作词
    ],
)
def test_guard_hits_department_not_in_his_catalog(text):
    verdict = assess_department_guard(text, KNOWN)
    assert verdict is not None
    assert verdict.mentioned in ("内科", "心脏外科")
    assert "本院没有" in verdict.reply
    assert "本院开设的科室有" in verdict.reply


@pytest.mark.parametrize(
    "text",
    [
        "预约挂号",                                       # 没有科室字段
        "上午",                                           # 补时段
        "有内科吗",                                       # 科室咨询，不是预约
        "心内科有哪些医生",                               # 专家查询
        "查询号源",
        "取消预约挂号",
        "帮我取消呼吸内科的预约",                         # 已给合法科室
        "你好",
        "你好，我胸痛",                                   # 急症，由医疗短路收走
        "感冒了怎么办",                                   # 科普
        "王新勃，610121199401032831，呼吸内科，2026-11-14",
        "王新勃，610121199401032831，心内科，2026.11.14",
    ],
)
def test_guard_passes_normal_conversation(text):
    assert assess_department_guard(text, KNOWN) is None


def test_empty_catalog_passes_instead_of_pretending_no_department():
    """HIS 主档不可用 ≠ 本院没有科室（his_doctor_service 的 fail-fast 教训）。"""
    assert assess_department_guard(FORM, []) is None
    assert assess_department_guard(FORM, ["", "  "]) is None


def test_suggestions_are_real_subspecialties_from_his():
    verdict = assess_department_guard(FORM, KNOWN)
    assert set(verdict.suggestions) == {"呼吸内科", "心内科", "消化内科", "神经内科"}
    assert all(s in KNOWN for s in verdict.suggestions)
    assert "与它相关的有" in verdict.reply


def test_fixed_reply_passes_medical_output_guard():
    """短路话术是纯信息，不能漏出诊断/用药/剂量（Phase 9-A 出口判据）。"""
    reply = assess_department_guard(FORM, KNOWN).reply
    assert not scan_medical_output(reply).hit
    for word in ("诊断", "剂量", "服用", "处方"):
        assert word not in reply


# ---------------------------------------------------------------- 引擎短路


@pytest.mark.parametrize("text", [FORM, "帮我挂内科的号", "我要预约心脏外科"])
async def test_invalid_department_short_circuits_without_model(text):
    model = FakeChatModel([])
    store = FakeStore()
    engine, metrics = make_engine(
        model, store=store, department_provider=lambda: KNOWN
    )
    expected = assess_department_guard(text, KNOWN).reply

    reply, events = await collect(engine, text)

    assert reply == expected
    assert model.calls == 0
    counters = metrics.snapshot()["counters"]
    assert counters["appointment_department_guard_total"] == 1
    assert counters["llm_calls_total"] == 0
    assert counters["tool_calls_total"] == 0
    deltas = [e for e in events if e.event_type == EventType.TEXT_RESPONSE]
    assert len(deltas) == 1, "固定话术应一次性下发，不能拆成多个气泡"
    assert deltas[0].metadata["department_guard"]["mentioned"]


async def test_short_circuit_reply_is_persisted():
    """话术必须落库，否则下一轮「那就呼吸内科吧」的上下文是断的。"""
    store = FakeStore()
    engine, _ = make_engine(
        FakeChatModel([]), store=store, department_provider=lambda: KNOWN
    )
    expected = assess_department_guard(FORM, KNOWN).reply

    await collect(engine, FORM)

    saved = getattr(store, "saved", None)
    assert saved is not None and len(saved) == 2  # user + assistant
    assert any(getattr(m, "content", "") == expected for m in saved)


async def test_guard_is_disabled_when_provider_not_injected():
    """默认 None = 既有行为零变更（777 条老测试不受影响）。"""
    answer = "正常回答"
    model = FakeChatModel([{"chunks": [answer], "turn": ChatTurn(text=answer)}])
    engine, metrics = make_engine(model)

    text, _ = await collect(engine, FORM)

    assert text == answer
    assert model.calls == 1
    assert metrics.snapshot()["counters"]["appointment_department_guard_total"] == 0


async def test_provider_failure_falls_back_to_model():
    """名单取数失败只告警并放行，绝不因守卫把请求打死。"""

    def broken():
        raise RuntimeError("HIS 主档连接断开")

    answer = "正常回答"
    model = FakeChatModel([{"chunks": [answer], "turn": ChatTurn(text=answer)}])
    engine, _ = make_engine(model, department_provider=broken)

    text, _ = await collect(engine, FORM)

    assert text == answer
    assert model.calls == 1


async def test_valid_department_still_reaches_model_and_tools():
    answer = "正在为您查询"
    model = FakeChatModel([{"chunks": [answer], "turn": ChatTurn(text=answer)}])
    engine, metrics = make_engine(model, department_provider=lambda: KNOWN)

    text, _ = await collect(
        engine, "王新勃，610121199401032831，呼吸内科，2026-11-14"
    )

    assert text == answer
    assert model.calls == 1
    assert metrics.snapshot()["counters"]["appointment_department_guard_total"] == 0


async def test_medical_safety_still_takes_priority():
    model = FakeChatModel([])
    engine, metrics = make_engine(model, department_provider=lambda: KNOWN)

    text, _ = await collect(engine, "你好，我胸痛")

    assert text == EMERGENCY_REPLY
    assert model.calls == 0
    assert metrics.snapshot()["counters"]["appointment_department_guard_total"] == 0


# ---------------------------------------------------------------- 审批预校验


def _book_tool(doctor_service):
    # 只测 prevalidate_args，不碰业务服务；构造时 repository/slot_service 不会被访问
    return BookAppointmentTool(object(), object(), doctor_service)


def test_book_tool_prevalidate_rejects_unknown_department():
    error = _book_tool(FakeDoctorService()).prevalidate_args({"department": "内科"})
    assert "本院没有" in error and "内科" in error


@pytest.mark.parametrize(
    "department", ["呼吸内科", "呼吸内科门诊", "", "   "]
)
def test_prevalidate_allows_known_normalized_or_missing_department(department):
    """缺字段/格式错留给工具层回传模型；这里只拦确定性的不存在。"""
    assert _book_tool(FakeDoctorService()).prevalidate_args(
        {"department": department}
    ) == ""


def test_prevalidate_falls_back_when_catalog_unavailable():
    assert _book_tool(FakeDoctorService(fail=True)).prevalidate_args(
        {"department": "内科"}
    ) == ""


class FakeApprovalService:
    def __init__(self):
        self.created = []

    def create(self, principal, session_id, tool_name, args, required_scopes=None):
        self.created.append({"tool_name": tool_name, "args": dict(args)})
        return SimpleNamespace(approval_id="ap-fake")

    def detail(self, approval_id, principal):
        return {"approval_id": approval_id, "args": {}}

    def consume(self, *args, **kwargs):
        raise AssertionError("预校验拦下的请求不该消费 token")


class WriteTool(Tool):
    """只实现审批门控需要的最小契约。"""

    def __init__(self, prevalidation_error=""):
        self.prevalidation_error = prevalidation_error
        self.call_count = 0

    def definition(self):
        return ToolDefinition(
            name="预约挂号",
            description="测试用写工具",
            parameters=[ToolParameter("department", "string", "科室")],
            risk_level=RiskLevel.WRITE,
        )

    def prevalidate_args(self, args):
        return self.prevalidation_error

    async def call(self, params):
        self.call_count += 1
        return ToolResult.ok("OK")


def _approval_executor():
    service = FakeApprovalService()
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={}),
        no_ui_policy="deny",
        approval_service=service,
    )
    return executor, service


async def test_invalid_department_never_creates_pending_approval():
    """核心护栏：PENDING 都不生成 → 前端不会弹注定失败的确认窗。"""
    executor, service = _approval_executor()
    tool = WriteTool("本院没有【内科】这个科室，可选科室有：呼吸内科…")

    result = await executor.execute(
        ToolCall(tool_name="预约挂号", args={"department": "内科"}), tool
    )

    assert not result.success
    assert result.error_type == "ValidationError"
    assert service.created == []
    assert tool.call_count == 0


async def test_valid_department_still_creates_pending_approval():
    """预校验不能把正常审批链路也吞掉。"""
    executor, service = _approval_executor()
    tool = WriteTool("")

    result = await executor.execute(
        ToolCall(tool_name="预约挂号", args={"department": "呼吸内科"}), tool
    )

    assert result.error_type == "ApprovalRequired"
    assert len(service.created) == 1
    assert tool.call_count == 0


# ---------------------------------------------------------------- 填充前缀


@pytest.mark.parametrize(
    "text,expected",
    [
        ("好的，好的，好的，", True),
        ("好嘞，请稍等，", True),
        ("没问题，马上", True),
        ("您好，", True),
        ("", True),
        # 带句末标点是完整句，必须保留（既有特征测试钉死的契约）
        ("好的。", False),
        ("好的，我先帮您查一下。", False),
        ("好的，我先帮您查一下，", False),
        ("很遗憾，", False),
        ("好的，查询结果显示还有号，", False),
    ],
)
def test_pure_filler_detection(text, expected):
    assert is_pure_filler(text) is expected
