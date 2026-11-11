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
from typing import Any, Callable, Dict, Optional

from xiaozhi.repositories.approval_repo import (
    ApprovalRecord,
    ApprovalRepository,
    ApprovalStatus,
)
from xiaozhi.security.principal import ANONYMOUS, Principal

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


_SENSITIVE_KEYS = ("id_card", "idcard", "identity", "身份证", "phone", "mobile", "手机号")


def mask_sensitive_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """生成**仅用于展示/落审批表**的脱敏副本。

    完整身份证号不进审批表、不回显前端（顺带收窄 P0-06 的一个泄露面；
    预约业务表本身仍明文，属于 Phase 3b 范围）。
    """
    preview: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        lowered = str(key).lower()
        if isinstance(value, str) and any(token in lowered for token in _SENSITIVE_KEYS):
            if len(value) > 8:
                preview[key] = f"{value[:6]}{'*' * (len(value) - 10)}{value[-4:]}"
            elif value:
                preview[key] = "*" * len(value)
            else:
                preview[key] = ""
        elif isinstance(value, (str, int, float, bool)) or value is None:
            preview[key] = value
        else:
            preview[key] = str(value)
    return preview


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

        now = self._now()
        record = ApprovalRecord(
            approval_id=f"ap-{secrets.token_urlsafe(18)}",
            subject_id=principal.subject_id,
            session_id=session_id,
            task_id=task_id,
            tool_name=tool_name,
            canonical_args_hash=args_hash,
            args_preview=mask_sensitive_args(canonical_args),
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
    ) -> ApprovalRecord:
        """执行前重新计算参数哈希并原子消费；任何不符一律抛 ApprovalRejected。"""
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
        return record

    def list_for_subject(self, principal: Principal) -> list:
        return [self.detail(r.approval_id, principal) for r in self.repository.list_for_subject(principal.subject_id)]
