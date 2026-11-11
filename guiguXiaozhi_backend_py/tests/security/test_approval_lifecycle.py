"""P0-02 审批状态机验收（离线，InMemory 仓储）。

对应审计《P0-02》7 条验收中与状态机直接相关的部分：
参数篡改 / 重放 / 超时 / 撤销 / 跨用户 / 重启一致 / 并发双花。
执行器侧的「无审批不执行」「未知风险拒绝」「OVERRIDE 不可由模型触发」
在 test_approval_executor.py 里覆盖。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from xiaozhi.repositories.approval_repo import (
    ApprovalStatus,
    InMemoryApprovalRepository,
)
from xiaozhi.security.approval_service import (
    ApprovalRejected,
    ApprovalService,
    canonical_args_hash,
    mask_sensitive_args,
)
from xiaozhi.security.principal import Principal

ALICE = Principal(subject_id="alice")
BOB = Principal(subject_id="bob")
ID_CARD = "610121199401032831"
ARGS = {
    "username": "李小明",
    "id_card": ID_CARD,
    "department": "心内科",
    "date": "2026-11-20",
    "time": "上午",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env():
    clock = FakeClock()
    repo = InMemoryApprovalRepository()
    service = ApprovalService(repo, ttl_seconds=120, clock=clock)
    return repo, service, clock


def _pending(env, args=None, principal=ALICE):
    _, service, _ = env
    return service.create(principal, "sess-1", "预约挂号", dict(args or ARGS))


def _approved_token(env, args=None, principal=ALICE):
    _, service, _ = env
    record = _pending(env, args, principal)
    return record, service.approve(record.approval_id, principal)


def test_create_is_pending_and_masks_id_card(env):
    """审批表与确认 UI 不得出现完整身份证号。"""
    record = _pending(env)
    assert record.status == ApprovalStatus.PENDING
    assert ID_CARD not in str(record.args_preview)
    assert "610121" in str(record.args_preview)
    assert "2831" in str(record.args_preview)
    assert mask_sensitive_args({"phone": "13800138000"})["phone"] == "138001*8000"


def test_create_is_idempotent_for_same_business_request(env):
    """同一主体/会话/工具/参数只产生一条待确认请求，避免重复弹框。"""
    first = _pending(env)
    second = _pending(env)
    assert first.approval_id == second.approval_id


def test_approve_once_and_reject_second_approve(env):
    _, service, _ = env
    record = _pending(env)
    token = service.approve(record.approval_id, ALICE)
    assert token
    assert service.repository.get(record.approval_id).token_hash
    assert token not in str(service.repository.get(record.approval_id).__dict__)
    with pytest.raises(ApprovalRejected):
        service.approve(record.approval_id, ALICE)


def test_consume_success_and_replay_rejected(env):
    """验收 3：一次性 token 第二次使用必须拒绝。"""
    _, service, _ = env
    record, token = _approved_token(env)
    consumed, effective = service.consume(
        token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
    )
    assert consumed.status == ApprovalStatus.CONSUMED
    assert effective == ARGS
    with pytest.raises(ApprovalRejected) as exc:
        service.consume(
            token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
        )
    assert exc.value.reason_code == "invalid_or_replayed"


def test_parameter_tampering_cannot_change_confirmed_args(env):
    """验收 2（语义更新）：模型改了参数 → **按用户确认的参数执行**。

    2026-11-11 真机事故：审批弹窗展示的是发起轮参数，用户确认后执行轮由
    模型重新生成 —— 7b 两轮输出不一致（发起轮带 doctor_name、执行轮漏了），
    硬比哈希会让"用户点了确认却被拒"。

    现在的语义：**用户确认的是哪组参数，系统就按哪组执行**。模型改参数
    改不出越权行为 —— 这比"直接拒绝"更安全（旧语义下模型把日期改对反而
    执行不了，用户白白确认）。
    """
    _, service, _ = env
    _, token = _approved_token(env)
    tampered = dict(ARGS, date="2026-11-21")
    assert canonical_args_hash(tampered) != canonical_args_hash(ARGS)
    record, effective = service.consume(
        token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=tampered
    )
    assert record.status == ApprovalStatus.CONSUMED
    assert effective == ARGS, "执行的必须是用户确认过的那组参数"
    assert effective["date"] != "2026-11-21", "模型的篡改生效了"


def test_legacy_record_without_canonical_args_still_rejects(env):
    """旧记录（没有存可执行参数）→ 维持严格拒绝（不放宽旧数据）。"""
    _, service, _ = env
    _, token = _approved_token(env)
    # 模拟旧数据：把审批记录里的可执行参数抹掉
    record = service.repository.get_by_token_hash(
        __import__("hashlib").sha256(token.encode("utf-8")).hexdigest()
    )
    assert record is not None
    record.canonical_args = None
    tampered = dict(ARGS, date="2026-11-21")
    with pytest.raises(ApprovalRejected):
        service.consume(
            token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=tampered
        )


def test_tool_or_session_change_invalidates_approval(env):
    _, service, _ = env
    _, token = _approved_token(env)
    with pytest.raises(ApprovalRejected):
        service.consume(
            token, ALICE, session_id="sess-1", tool_name="取消预约挂号", canonical_args=ARGS
        )
    _, token2 = _approved_token(env, args=dict(ARGS, date="2026-12-01"))
    with pytest.raises(ApprovalRejected):
        service.consume(
            token2, ALICE, session_id="sess-2", tool_name="预约挂号",
            canonical_args=dict(ARGS, date="2026-12-01"),
        )


def test_cross_user_approve_detail_and_consume_rejected(env):
    """验收 4：跨用户一律拒绝，且不泄露归属信息。"""
    _, service, _ = env
    record = _pending(env)
    with pytest.raises(ApprovalRejected) as exc:
        service.approve(record.approval_id, BOB)
    assert exc.value.reason_code == "forbidden"
    with pytest.raises(ApprovalRejected):
        service.detail(record.approval_id, BOB)
    _, token = _approved_token(env)
    with pytest.raises(ApprovalRejected):
        service.consume(
            token, BOB, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
        )


def test_expired_approval_rejected_and_marked_expired(env):
    """验收 4：超时拒绝，状态自动迁移到 EXPIRED。"""
    repo, service, clock = env
    record, token = _approved_token(env)
    clock.advance(121)
    with pytest.raises(ApprovalRejected):
        service.consume(
            token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
        )
    assert repo.get(record.approval_id).status in (
        ApprovalStatus.EXPIRED,
        ApprovalStatus.APPROVED,
    )  # consume 不改状态；再查详情时由 _expire_if_needed 迁移
    assert service.detail(record.approval_id, ALICE)["status"] == "EXPIRED"


def test_revoke_invalidates_token(env):
    """验收 4：撤销后即便持有明文 token 也不能执行。"""
    _, service, _ = env
    record, token = _approved_token(env)
    service.revoke(record.approval_id, ALICE)
    with pytest.raises(ApprovalRejected):
        service.consume(
            token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
        )


def test_state_survives_service_restart(env):
    """验收 5：换一个 Service 实例（模拟进程重启/多实例）状态仍一致。"""
    repo, service, clock = env
    record, token = _approved_token(env)
    restarted = ApprovalService(repo, ttl_seconds=120, clock=clock)
    assert restarted.detail(record.approval_id, ALICE)["status"] == "APPROVED"
    consumed, _effective = restarted.consume(
        token, ALICE, session_id="sess-1", tool_name="预约挂号", canonical_args=ARGS
    )
    assert consumed.status == ApprovalStatus.CONSUMED


def test_concurrent_consume_only_one_winner(env):
    """验收 5 的并发部分：20 个并发消费只有一个能拿到授权。"""
    _, service, _ = env
    _, token = _approved_token(env)

    def attempt(_: int) -> bool:
        try:
            service.consume(
                token, ALICE, session_id="sess-1", tool_name="预约挂号",
                canonical_args=ARGS,
            )
            return True
        except ApprovalRejected:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        winners = list(pool.map(attempt, range(20)))
    assert sum(winners) == 1
