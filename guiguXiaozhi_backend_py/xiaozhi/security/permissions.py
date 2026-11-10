"""权限决策引擎 —— 对齐参考项目 mini_harness/security/permissions.py。

策略**外置**为 YAML（config/permissions.yaml），改策略不用改代码。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class PermissionLevel(str, Enum):
    """权限等级

    与参考项目的语义有一处**刻意差异**：参考项目里 AUTO 表示"首次询问、之后缓存授权"，
    但硅谷小智是聊天应用，没有独立的授权 UI —— 让模型问"我可以查号源吗"很奇怪。
    因此这里把 AUTO 定义为"直接放行"（给只读工具用），
    ASK 才是"需要授权"，在无交互通道时按 no_ui 策略处理。
    """

    DENY = "deny"          # 永远不允许
    ASK = "ask"            # 需要授权（无 UI 时按 no_ui 策略）
    AUTO = "auto"          # 直接放行（只读、无副作用）
    OVERRIDE = "override"  # 管理员强制放行


class Decision(str, Enum):
    """权限决策结果"""

    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


@dataclass
class AuditEntry:
    """审计日志条目"""

    user_id: str
    tool_name: str
    decision: str
    reason: str = ""
    timestamp: str = ""
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tool_name": self.tool_name,
            "decision": self.decision,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


def approval_scope_for_args(args: Dict[str, Any]) -> str:
    """用参数生成稳定的授权指纹（同样的参数视为同一次授权）"""
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PermissionDecisionEngine:
    """基于策略的权限决策引擎"""

    DEFAULT_SCOPE = "__tool__"

    def __init__(
        self,
        policies: Optional[Dict[str, PermissionLevel]] = None,
        approval_ttl_seconds: float = 300.0,
    ) -> None:
        self._policies: Dict[str, PermissionLevel] = dict(policies or {})
        # 授权缓存：(user_id, tool_name, 参数指纹) -> 授权时刻（monotonic）。
        # 2026-11-10 审计修复：此前 _approvals 只在 record_approval 里写入、
        # decide() 从不查询 —— 授权是死数据，ASK 工具每次都要重新确认。
        # 现在缓存授权带 TTL，过期自动失效。
        self._approvals: Dict[Tuple[str, str, str], float] = {}
        self.approval_ttl_seconds = max(0.0, float(approval_ttl_seconds))
        self._audit: List[AuditEntry] = []
        self._lock = threading.Lock()

    # ---------- 策略 ----------

    def register_policy(
        self, tool_name: str, level: PermissionLevel, description: str = ""
    ) -> None:
        with self._lock:
            self._policies[tool_name] = level

    def policy_of(self, tool_name: str) -> PermissionLevel:
        """未登记的工具默认 DENY —— 默认拒绝原则（纵深防御）"""
        return self._policies.get(tool_name, PermissionLevel.DENY)

    @classmethod
    def from_yaml(cls, path: Path) -> "PermissionDecisionEngine":
        """从 YAML 加载策略"""
        engine = cls()
        if not path.exists():
            return engine
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ttl = data.get("approval_ttl_seconds")
        if ttl is not None:
            engine.approval_ttl_seconds = max(0.0, float(ttl))
        for name, spec in (data.get("tools") or {}).items():
            level = spec.get("level", "ask") if isinstance(spec, dict) else str(spec)
            try:
                engine.register_policy(name, PermissionLevel(level))
            except ValueError:
                continue
        return engine

    # ---------- 决策 ----------

    def decide(
        self,
        tool_name: str,
        user_id: str,
        approval_scope: Optional[str] = None,
        trace_id: str = "",
    ) -> Decision:
        """做一次权限决策"""
        level = self.policy_of(tool_name)
        scope = approval_scope or self.DEFAULT_SCOPE

        if level == PermissionLevel.DENY:
            self._log(user_id, tool_name, Decision.DENY, "策略：禁止", trace_id)
            return Decision.DENY

        if level == PermissionLevel.OVERRIDE:
            self._log(user_id, tool_name, Decision.ALLOW, "策略：强制放行", trace_id)
            return Decision.ALLOW

        if level == PermissionLevel.AUTO:
            # 只读工具直接放行（见 PermissionLevel 的说明）
            self._log(user_id, tool_name, Decision.ALLOW, "策略：自动放行", trace_id)
            return Decision.ALLOW

        # ASK：先查缓存授权（同 user + 同工具 + 同参数指纹，且未过 TTL）
        # —— 此前这段查询缺失，授权记录成了死数据（2026-11-10 审计修复）
        if self._was_approved_before(user_id, tool_name, scope):
            self._log(
                user_id, tool_name, Decision.ALLOW, "命中缓存授权（参数指纹一致）", trace_id
            )
            return Decision.ALLOW
        self._log(user_id, tool_name, Decision.ASK_USER, "策略：需确认", trace_id)
        return Decision.ASK_USER

    def record_approval(
        self,
        user_id: str,
        tool_name: str,
        approval_scope: Optional[str] = None,
        trace_id: str = "",
    ) -> None:
        """记录一次用户授权（带 TTL，过期自动失效）"""
        scope = approval_scope or self.DEFAULT_SCOPE
        with self._lock:
            self._approvals[(user_id, tool_name, scope)] = time.monotonic()
        self._log(user_id, tool_name, Decision.ALLOW, "用户已授权", trace_id)

    def _was_approved_before(
        self, user_id: str, tool_name: str, approval_scope: Optional[str] = None
    ) -> bool:
        scope = approval_scope or self.DEFAULT_SCOPE
        key = (user_id, tool_name, scope)
        with self._lock:
            granted_at = self._approvals.get(key)
            if granted_at is None:
                return False
            if time.monotonic() - granted_at > self.approval_ttl_seconds:
                del self._approvals[key]
                return False
            return True

    def clear_approvals(self) -> None:
        """清空授权缓存（测试 / 运维用）"""
        with self._lock:
            self._approvals.clear()

    # ---------- 审计 ----------

    def _log(
        self,
        user_id: str,
        tool_name: str,
        decision: Decision,
        reason: str,
        trace_id: str = "",
    ) -> None:
        from datetime import datetime

        entry = AuditEntry(
            user_id=user_id,
            tool_name=tool_name,
            decision=decision.value,
            reason=reason,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            trace_id=trace_id,
        )
        with self._lock:
            self._audit.append(entry)

    def get_audit_logs(self) -> List[AuditEntry]:
        with self._lock:
            return list(self._audit)

    def clear_audit_logs(self) -> None:
        with self._lock:
            self._audit.clear()
