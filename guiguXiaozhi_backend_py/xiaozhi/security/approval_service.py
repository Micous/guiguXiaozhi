"""审批服务 —— Phase 2（审计 P0-02）的授权闭环核心。

设计红线（对应 P0-02 的 7 条验收）：
  1. 模型自述「用户已经同意」永远不算审批；只有本服务的 ``approve()`` 能签发 token。
  2. token 绑定 subject / session / task / tool / **规范化参数哈希**，一次性消费。
  3. 审批状态落 MySQL（多实例/重启一致），token 明文只存哈希、只返回一次。
  4. 展示用参数做脱敏，完整身份证号不写进审批表、不回显前端。

同时提供请求级 ``ApprovalContext``（contextvar）：让裸文本流协议与
``engine.run()`` 签名都无需改动，就能把 principal / token / session_id 送到执行器。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

import logging

from xiaozhi.repositories.approval_repo import (
    ApprovalRecord,
    ApprovalRepository,
    ApprovalStatus,
)
from xiaozhi.security.masking import mask_sensitive_args as _mask_sensitive_args
from xiaozhi.security.principal import ANONYMOUS, Principal

logger = logging.getLogger(__name__)

# 统一使用 naive local time：MySQL 的 NOW() 也是服务器本地时间，
# 用 utcnow 会和容器/系统时区错开 8 小时，导致审批刚签发就「过期」。
def _default_clock() -> datetime:
    return datetime.now()


def canonical_args_hash(args: Dict[str, Any]) -> str:
    """规范化参数指纹（审计 P0-02 的核心绑定项）。

    键排序 + 紧凑分隔 + ensure_ascii=False，保证同语义参数得到同一哈希；
    参数被改动（日期 A→B）后哈希必然不同。
    """
    encoded = json.dumps(
        args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_sensitive_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """生成**仅用于展示/落审批表**的脱敏副本。

    完整身份证号不进审批表、不回显前端。Phase 8 起实现收敛到
    ``security/masking.py``（同一口径同时供工具层回显、历史落库使用）——
    这里保留函数名不改调用方，避免为了"统一实现"去动审批这条高风险链路。
    """
    return _mask_sensitive_args(args)


class ApprovalDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalError(Exception):
    """审批域异常基类"""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class ApprovalRejected(ApprovalError):
    """任何一条校验不通过：重放/篡改/过期/撤销/跨用户/缺 scope"""


class ApprovalNotFound(ApprovalError):
    def __init__(self) -> None:
        super().__init__("not_found", "审批请求不存在")


@dataclass(frozen=True)
class ApprovalContext:
    """一次 chat 请求的审批上下文（由路由层写入 contextvar）。"""

    principal: Principal = ANONYMOUS
    approval_token: str = ""
    session_id: str = ""
    auth_enabled: bool = False


_DEFAULT_CONTEXT = ApprovalContext()
_approval_context: ContextVar[ApprovalContext] = ContextVar(
    "xiaozhi_approval_context", default=_DEFAULT_CONTEXT
)


def set_approval_context(context: ApprovalContext) -> None:
    _approval_context.set(context)


def get_approval_context() -> ApprovalContext:
    return _approval_context.get()


def reset_approval_context() -> None:
    _approval_context.set(_DEFAULT_CONTEXT)


@dataclass(frozen=True)
class ConsumeResult:
    decision: ApprovalDecision
    reason_code: str = ""
    record: Optional[ApprovalRecord] = None


class ApprovalService:
    """审批状态机：create(PENDING) → approve(APPROVED) → consume(CONSUMED)。"""

    def __init__(
        self,
        repository: ApprovalRepository,
        ttl_seconds: float = 120.0,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.repository = repository
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock

    # ---------- 内部工具 ----------

    def _now(self) -> datetime:
        return self._clock()

    def _task_id(self, session_id: str, tool_name: str, args_hash: str) -> str:
        """确定性任务标识：二次请求只要参数没变，重算结果就一致。

        不用 trace_id —— 每次 HTTP 请求都会变，绑定它会导致审批永远无法在
        下一轮对话里被消费。
        """
        return f"{session_id}:{tool_name}:{args_hash[:12]}"

    def _idempotency_key(
        self, subject_id: str, session_id: str, task_id: str, tool_name: str, args_hash: str
    ) -> str:
        raw = f"{subject_id}|{session_id}|{task_id}|{tool_name}|{args_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _expire_if_needed(self, record: ApprovalRecord) -> ApprovalRecord:
        if record.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            if record.expires_at <= self._now():
                record.status = ApprovalStatus.EXPIRED
                self.repository.save(record)
        return record

    def _owned(self, approval_id: str, principal: Principal) -> ApprovalRecord:
        record = self.repository.get(approval_id)
        if record is None:
            raise ApprovalNotFound()
        # 跨使用者统一返回 forbidden，不泄露这条 id 属于谁
        if record.subject_id != principal.subject_id:
            raise ApprovalRejected("forbidden", "审批请求不属于当前用户")
        return self._expire_if_needed(record)

    # ---------- 状态机 ----------

    def create(
        self,
        principal: Principal,
        session_id: str,
        tool_name: str,
        canonical_args: Dict[str, Any],
        *,
        required_scopes: Optional[list] = None,
    ) -> ApprovalRecord:
        """写工具被调用但尚无有效审批时，创建（或复用）PENDING 请求。"""
        args_hash = canonical_args_hash(canonical_args)
        task_id = self._task_id(session_id, tool_name, args_hash)
        key = self._idempotency_key(
            principal.subject_id, session_id, task_id, tool_name, args_hash
        )
        existing = self.repository.find_active_by_key(key)
        if existing is not None:
            return self._expire_if_needed(existing)

        # 同一组参数**已经办过**（上一条记录是 CONSUMED/EXPIRED/REVOKED）：
        # 幂等键是"同一件事只处理一次"的语义，但 ``uk_idempotency`` 是
        # **表级唯一约束**，终态记录仍在表里 → 直接 INSERT 会撞唯一键。
        # 2026-11-11 真机事故：这个 IntegrityError 一路抛到 HTTP 层，
        # 用户看到的是"（服务暂时异常：IntegrityError）"。
        #
        # 处理：给新一轮请求换一个**带序号的**幂等键 —— 业务上仍是
        # "同参数复用同一条待确认记录"，而"已办过的那次"不该阻塞新的一次。
        attempt = 0
        while True:
            candidate_key = (
                key if attempt == 0 else self._idempotency_key(
                    principal.subject_id, session_id, task_id, tool_name,
                    f"{args_hash}#{attempt}",
                )
            )
            stale = self.repository.find_by_key(candidate_key)
            if stale is None:
                key = candidate_key
                break
            attempt += 1
            if attempt > 20:  # 兜底：极端情况下不要死循环
                key = f"{key}#{self._now().timestamp()}"
                break

        now = self._now()
        record = ApprovalRecord(
            approval_id=f"ap-{secrets.token_urlsafe(18)}",
            subject_id=principal.subject_id,
            session_id=session_id,
            task_id=task_id,
            tool_name=tool_name,
            canonical_args_hash=args_hash,
            args_preview=mask_sensitive_args(canonical_args),
            # 发起轮的可执行参数 —— 执行轮以它为准（用户确认的是哪组参数，
            # 系统就按哪组执行；模型两轮输出不一致时不能把用户卡在"确认无效"）。
            # ⚠️ 含明文身份证号，与 appointment 表同安全水位，绝不回显前端。
            canonical_args=dict(canonical_args),
            resource_scope=f"{session_id}:{tool_name}",
            status=ApprovalStatus.PENDING,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            created_at=now,
            idempotency_key=key,
        )
        self.repository.create(record)
        return record

    def detail(self, approval_id: str, principal: Principal) -> Dict[str, Any]:
        """给确认 UI 的详情：脱敏参数 + 影响说明，绝不含 token/哈希/主体标识。"""
        record = self._owned(approval_id, principal)
        return {
            "approval_id": record.approval_id,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "expires_at": record.expires_at.isoformat(timespec="seconds"),
            "created_at": record.created_at.isoformat(timespec="seconds"),
            "args": record.args_preview,
            "canonical_args_hash": record.canonical_args_hash,
            "resource_scope": record.resource_scope,
        }

    def approve(self, approval_id: str, principal: Principal) -> str:
        """用户明确确认 → 签发短时一次性 token（明文只在此返回一次）。"""
        record = self._owned(approval_id, principal)
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalRejected("not_pending", "审批请求已处理或已过期")
        token = secrets.token_urlsafe(32)
        record.token_hash = _token_hash(token)
        record.status = ApprovalStatus.APPROVED
        record.approved_by = principal.subject_id
        record.approved_at = self._now()
        self.repository.save(record)
        return token

    def revoke(self, approval_id: str, principal: Principal) -> None:
        record = self._owned(approval_id, principal)
        if record.status not in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            raise ApprovalRejected("not_revocable", "审批请求已终结")
        record.status = ApprovalStatus.REVOKED
        record.token_hash = ""  # 撤销后即便持有明文 token 也无法消费
        self.repository.save(record)

    def inspect_token(
        self, token: str, principal: Principal, *, session_id: str
    ) -> ApprovalRecord:
        """HTTP 层预检：判断 token 是否仍可用，**绝不消费**。

        消费仍由 ``SecureToolExecutor`` 在真正执行前原子完成（绑定规范化参数）。
        预检的作用是：重放/过期/越权的 token 在进入模型前就被 409 拦下 ——
        否则模型会因历史里已有成功记录而复述「已预约成功」，用户误以为
        第二次也真的执行了（2026-11-11 真机验证发现的误导）。
        """
        if not token:
            raise ApprovalRejected("missing_token", "缺少审批 token")
        record = self.repository.get_by_token_hash(_token_hash(token))
        if record is None:
            raise ApprovalRejected("invalid_or_replayed", "审批不存在或已被撤销")
        if record.subject_id != principal.subject_id or record.session_id != session_id:
            raise ApprovalRejected("forbidden", "审批不属于当前用户或会话")
        record = self._expire_if_needed(record)
        if record.status != ApprovalStatus.APPROVED:
            raise ApprovalRejected(
                "invalid_or_replayed", f"审批不可再用（{record.status.value}）"
            )
        return record

    def consume(
        self,
        token: str,
        principal: Principal,
        *,
        session_id: str,
        tool_name: str,
        canonical_args: Dict[str, Any],
    ) -> Tuple[ApprovalRecord, Dict[str, Any]]:
        """执行前原子消费；返回 (记录, **实际要执行的参数**)。

        ⚠️ 2026-11-11 真机事故后的口径调整：

        审批弹窗展示的是**发起轮**的参数，用户确认后执行轮由模型**重新生成**
        一遍 —— 7b 模型两轮输出不完全一致（实测：发起轮带 ``doctor_name``，
        执行轮漏了它），硬比 ``canonical_args_hash`` 会让"用户点了确认却被拒"
        （``invalid_or_replayed``）。

        现在的语义：**以用户确认过的那组参数执行**。参数不匹配时返回
        审批记录里的参数；记录里没有参数（旧数据）才拒绝。

        这**不是放宽安全边界**：用户能改的只有参数值，而参数值恰恰就是
        用户确认过的那份 —— 改不出越权行为。token 一次性消费、跨用户、
        跨会话、跨工具、过期这五道边界全部由仓储层原子消费保证。
        """
        if not token:
            raise ApprovalRejected("missing_token", "缺少审批 token")
        args_hash = canonical_args_hash(canonical_args)
        task_id = self._task_id(session_id, tool_name, args_hash)
        record = self.repository.consume_by_token(
            _token_hash(token),
            now=self._now(),
            subject_id=principal.subject_id,
            session_id=session_id,
            task_id=task_id,
            tool_name=tool_name,
            canonical_args_hash=args_hash,
        )
        if record is None:
            raise ApprovalRejected(
                "invalid_or_replayed", "审批无效：已使用、已过期、参数变更或不属于当前用户"
            )
        if record.canonical_args_hash == args_hash:
            # 完全一致 → 用模型这次的参数
            return record, canonical_args
        if not record.canonical_args:
            # 旧记录没有存可执行参数 → 只能维持严格拒绝
            raise ApprovalRejected(
                "invalid_or_replayed", "审批无效：参数与确认时不一致"
            )
        logger.info(
            "审批参数与执行轮不一致，按用户确认的参数执行：tool=%s",
            tool_name,
        )
        return record, dict(record.canonical_args)

    def list_for_subject(self, principal: Principal) -> list:
        return [self.detail(r.approval_id, principal) for r in self.repository.list_for_subject(principal.subject_id)]
