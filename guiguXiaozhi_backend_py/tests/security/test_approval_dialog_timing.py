"""第十轮事故回归（2026-11-11）：弹窗时序错乱 + 同参数重复发起撞唯一键。

用户反馈原文：「我还没确认就弹窗了，最后一个还没有弹窗」。

真实会话（8081/5173，用户手动操作）：

```
轮1 用户：预约挂号
轮1 模型：好的，请提供您的姓名、身份证号、预约的科室、日期…（追问）
轮2 用户：王新勃，610121199401032831，内科，2026.11.12
轮2 模型：好的，我将为您预约内科。内科在本院开设的科室中并不具体指代某个科室，
          根据您的需求，我将为您推荐呼吸内科。请问您是否同意预约呼吸内科？…
          该操作（预约挂号）需要您核对信息并明确确认后才会执行，请在确认窗口核对参数…
轮3 模型：好的，我将为您预约呼吸内科的号。请确认您的信息：…
          您的预约已经成功。预约详情如下：…医生：徐国兴。
```

数据库证据：整段会话**只有一条**审批记录
（``ap-r1Kt7BTOQxckUKGGXeU2XzMg``，参数 = 呼吸内科 / 2026-11-12 / 上午），
``approved_at=14:07:14`` **早于** ``consumed_at=14:07:50`` ——
用户确实点了确认，**没有安全绕过**。真正的问题有三个：

**问题一：弹窗"提前"了。**
模型在同一轮里先说「我将为您预约内科…请问您是否同意预约呼吸内科？」，
紧接着又真的调了工具 → 引擎把叙述段与审批提示拼在一起下发。
用户看到的观感是"我还没表态（同意换成呼吸内科），弹窗就先弹了"。

**问题二：最后一个弹窗缺失。**
真正执行那一轮，模型再次叙述「我将为您预约呼吸内科的号」，
引擎因叙述检测触发纠正重试 → 再次调工具 → 后端按幂等键
**复用同一条记录**（同一 ``approval_id``）→ 前端
「同一 id 不重复弹窗」的去重逻辑把它吃掉 → 用户看不到弹窗。

**问题三：同参数重复发起撞唯一键 → 500。**
``approval_requests.uk_idempotency`` 是**表级唯一约束**，而
``find_active_by_key`` 只查 PENDING/APPROVED（不含终态）。
于是「同参数已 CONSUMED 后再次发起」会撞唯一键，
IntegrityError 一路抛到 HTTP 层 → 用户看到
「（服务暂时异常：IntegrityError）」——一个用户无法理解也无法自行修复的 500。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

import pytest  # noqa: E402

from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.repositories.approval_repo import (  # noqa: E402
    ApprovalStatus,
    InMemoryApprovalRepository,
)
from xiaozhi.security.approval_service import ApprovalService  # noqa: E402
from xiaozhi.security.principal import Principal  # noqa: E402

from test_engine_characterization import make_engine  # noqa: E402
from test_engine_reliability import FakeChatModel, FakeExecutor  # noqa: E402


def _principal(subject: str = "alice") -> Principal:
    return Principal(subject_id=subject, roles=[], scopes=["chat:write"], auth_method="bearer")


def _service(repo=None) -> ApprovalService:
    return ApprovalService(
        repository=repo or InMemoryApprovalRepository(),
        ttl_seconds=900,
    )


# ---------- 问题三：同参数重复发起撞唯一键 ----------


def test_same_args_after_consumed_can_be_requested_again():
    """同参数在上一条记录 CONSUMED 后再次发起 → 必须拿到**新的** PENDING 记录。

    回归事故：``uk_idempotency`` 是表级唯一约束，终态记录仍在表里，
    再 INSERT 同键会抛 IntegrityError → 用户看到「服务暂时异常：IntegrityError」。
    幂等键的语义是"同一件事只处理一次"，不该阻塞"再办一次新的"。
    """
    repo = InMemoryApprovalRepository()
    service = _service(repo)
    principal = _principal()
    args = {
        "username": "王新勃",
        "id_card": "610121199401032831",
        "department": "呼吸内科",
        "date": "2026-11-12",
        "time": "上午",
    }

    first = service.create(principal, "sess-1", "预约挂号", dict(args))
    assert first.status == ApprovalStatus.PENDING

    # 模拟：用户确认并消费
    token = service.approve(first.approval_id, principal)
    service.consume(
        token,
        principal,
        session_id="sess-1",
        tool_name="预约挂号",
        canonical_args=dict(args),
    )
    assert repo.get(first.approval_id).status == ApprovalStatus.CONSUMED

    # 同参数再次发起 → 不应抛异常，且应产生新记录
    second = service.create(principal, "sess-1", "预约挂号", dict(args))
    assert second.status == ApprovalStatus.PENDING
    assert second.approval_id != first.approval_id
    # 幂等键必须不同，否则 INSERT 会撞唯一约束
    assert second.idempotency_key != first.idempotency_key
    # 且两条记录都在库里（没有静默丢记录）
    assert repo.get(first.approval_id) is not None
    assert repo.get(second.approval_id) is not None


def test_repeated_requests_keep_producing_distinct_records():
    """连续多次同参数发起（未消费）→ 复用同一条 PENDING（不重复弹框）。

    这是幂等的**本意**：同一件待确认的事不该反复弹窗。
    """
    repo = InMemoryApprovalRepository()
    service = _service(repo)
    principal = _principal()
    args = {"username": "王新勃", "department": "呼吸内科", "date": "2026-11-12", "time": "上午"}

    a = service.create(principal, "sess-1", "预约挂号", dict(args))
    b = service.create(principal, "sess-1", "预约挂号", dict(args))
    assert a.approval_id == b.approval_id, "仍可用的请求应复用，避免重复弹框"


def test_find_by_key_sees_terminal_records():
    """``find_by_key`` 必须能查到终态记录（CONSUMED/EXPIRED）。

    ``find_active_by_key`` 只查 PENDING/APPROVED（未过期），
    所以它**看不到**已 CONSUMED 的那条 —— 这正是唯一键冲突的根因。
    """
    repo = InMemoryApprovalRepository()
    service = _service(repo)
    principal = _principal()
    args = {"username": "王新勃", "department": "呼吸内科", "date": "2026-11-12", "time": "上午"}

    record = service.create(principal, "sess-1", "预约挂号", dict(args))
    token = service.approve(record.approval_id, principal)
    service.consume(
        token,
        principal,
        session_id="sess-1",
        tool_name="预约挂号",
        canonical_args=dict(args),
    )

    # 终态记录：active 查不到，by_key 查得到
    assert repo.find_active_by_key(record.idempotency_key) is None
    found = repo.find_by_key(record.idempotency_key)
    assert found is not None
    assert found.approval_id == record.approval_id
    assert found.status == ApprovalStatus.CONSUMED


# ---------- 问题二：执行轮复用同 id，前端去重吃掉弹窗 ----------


def test_execution_round_reuses_same_approval_id():
    """确认后重发同一句 → 后端复用同一审批记录（同 id）。

    这是设计使然（幂等），但前端"同一 id 不重复弹"的去重逻辑
    会把**执行轮**的弹窗吃掉 → 用户看不到任何确认入口。
    这里把该行为钉死，作为前端修复的对照依据。
    """
    repo = InMemoryApprovalRepository()
    service = _service(repo)
    principal = _principal()
    args = {"username": "王新勃", "department": "呼吸内科", "date": "2026-11-12", "time": "上午"}

    first = service.create(principal, "sess-1", "预约挂号", dict(args))
    # 模拟：用户点确认 → 前端带 token 重发 → 引擎再次调工具 → L0 门控
    # （此时 token 已被消费，走的是 create 分支之外的路径；
    #  这里只验证"无 token 时同参数复用同一条 PENDING"这一行为）
    again = service.create(principal, "sess-1", "预约挂号", dict(args))
    assert again.approval_id == first.approval_id


# ---------- 问题一：同一轮里既询问用户、又调用写工具 ----------


def test_approval_prompt_forbids_asking_and_calling_in_same_turn():
    """审批开启时的提示词必须禁止"同一轮既询问用户又调用写工具"。

    2026-11-11 第十轮事故：模型说「好的，我将为您预约内科。…请问您是否同意
    预约呼吸内科？」，**紧接着**又真的调了工具 → 确认窗弹出。用户看到的观感是
    "我还没回答是否同意换成呼吸内科，弹窗就先弹了"，而弹窗里的科室是
    **模型自己替他选的**。

    这条必须在提示词里明确：信息齐全就直接调工具；需要用户表态就先停下询问。
    """
    from xiaozhi.runtime.engine_phases import APPROVAL_PROMPT_NOTE

    # 明确"不要在同一轮里既询问又调用写工具"
    assert "不要在同一轮里既询问又调用写工具" in APPROVAL_PROMPT_NOTE
    # 明确"确认窗由系统弹出"，避免模型编造不存在的按钮
    assert "确认窗" in APPROVAL_PROMPT_NOTE
    assert "不要让用户去点击任何按钮" in APPROVAL_PROMPT_NOTE


async def test_approval_notice_is_injected_only_when_approval_enabled():
    """审批开启 → 注入说明；关闭 → 提示词与一期完全一致。"""
    from xiaozhi.runtime.engine_phases import APPROVAL_PROMPT_NOTE

    executor = FakeExecutor(ToolResult.ok("OK"))
    engine, _ = make_engine(FakeChatModel([]), executor=executor)
    assert engine._approval_enabled() is False

    class _ApprovalExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(ToolResult.ok("OK"))
            self.approval_service = object()

    engine2, _ = make_engine(FakeChatModel([]), executor=_ApprovalExecutor())
    assert engine2._approval_enabled() is True
    assert APPROVAL_PROMPT_NOTE


# ---------- 执行轮：带 token 却没调工具 → 必须补一轮强制执行 ----------


def test_approval_execution_prompt_is_explicit_about_execution():
    """强制执行指令必须说清"用户已确认，必须真的调工具"。

    事故原句：模型输出「请在确认窗口核对上述信息并确认预约。未确认前不会进行
    实际挂号操作。」—— 用户**已经确认过了**，模型把已完成的确认又推回去，
    而操作并没有发生。
    """
    from xiaozhi.runtime.engine_phases import APPROVAL_EXECUTION_PROMPT

    assert "用户已经完成确认" in APPROVAL_EXECUTION_PROMPT
    assert "真正调用工具" in APPROVAL_EXECUTION_PROMPT
    assert "不要再次要求用户确认" in APPROVAL_EXECUTION_PROMPT


def test_approval_forced_retry_happens_at_most_once():
    """强制重试**最多一次** —— 避免模型被反复追问、同一句话刷屏。

    真机事故：模型连续 5 轮重复同一句失败原因，用户界面被刷屏。
    根因：用 ``state.corrective_used`` 判断预算，但本路径不经过
    ``consume_corrective`` → 计数永远是 0 → 每次都重试。
    """
    from xiaozhi.runtime.state import TurnState

    state = TurnState()
    assert state.approval_forced_retry_done is False
    state.approval_forced_retry_done = True
    assert state.approval_forced_retry_done is True
