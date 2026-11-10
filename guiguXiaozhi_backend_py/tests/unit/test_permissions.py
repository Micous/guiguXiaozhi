"""权限决策引擎单测 —— 授权缓存打通 / TTL / no_ui 兜底校验。

2026-11-10 审计发现：record_approval 记录的授权从不被 decide() 查询，
ASK 工具每次都要重新确认 —— 授权缓存是死数据。本文件锁定修复后的契约。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.security.permissions import (  # noqa: E402
    Decision,
    PermissionDecisionEngine,
    PermissionLevel,
    approval_scope_for_args,
)
from xiaozhi.security.secure_executor import SecureToolExecutor  # noqa: E402


def _engine(**kwargs) -> PermissionDecisionEngine:
    return PermissionDecisionEngine(
        policies={"预约挂号": PermissionLevel.ASK}, **kwargs
    )


def test_recorded_approval_is_consulted_by_decide():
    """授权后同参数指纹的 ASK 工具 → 直接放行（修复前永远 ASK_USER）"""
    engine = _engine()
    scope = approval_scope_for_args({"username": "王新勃", "date": "2026-11-20"})

    assert engine.decide("预约挂号", "u1", scope) == Decision.ASK_USER

    engine.record_approval("u1", "预约挂号", scope)
    assert engine.decide("预约挂号", "u1", scope) == Decision.ALLOW


def test_approval_is_bound_to_argument_scope():
    """参数指纹不同的调用 → 授权不覆盖，仍需确认"""
    engine = _engine()
    scope_a = approval_scope_for_args({"date": "2026-11-20"})
    scope_b = approval_scope_for_args({"date": "2026-11-21"})

    engine.record_approval("u1", "预约挂号", scope_a)

    assert engine.decide("预约挂号", "u1", scope_a) == Decision.ALLOW
    assert engine.decide("预约挂号", "u1", scope_b) == Decision.ASK_USER


def test_approval_is_bound_to_user_and_tool():
    engine = _engine()
    scope = approval_scope_for_args({"date": "2026-11-20"})
    engine.record_approval("u1", "预约挂号", scope)

    assert engine.decide("预约挂号", "u2", scope) == Decision.ASK_USER


def test_approval_ttl_expiry():
    """TTL 过期 → 授权失效，回到 ASK_USER"""
    engine = _engine(approval_ttl_seconds=0.01)
    scope = approval_scope_for_args({"date": "2026-11-20"})

    engine.record_approval("u1", "预约挂号", scope)
    assert engine.decide("预约挂号", "u1", scope) == Decision.ALLOW

    time.sleep(0.05)
    assert engine.decide("预约挂号", "u1", scope) == Decision.ASK_USER


def test_auto_and_deny_unaffected_by_approvals():
    """AUTO 永远放行、DENY 永远拒绝——与授权缓存无关"""
    engine = PermissionDecisionEngine(
        policies={"查询是否有号源": PermissionLevel.AUTO, "危险工具": PermissionLevel.DENY}
    )
    engine.record_approval("u1", "危险工具", engine.DEFAULT_SCOPE)

    assert engine.decide("查询是否有号源", "u1") == Decision.ALLOW
    assert engine.decide("危险工具", "u1", engine.DEFAULT_SCOPE) == Decision.DENY


def test_from_yaml_reads_ttl():
    """YAML 顶层 approval_ttl_seconds 可配授权缓存时长"""
    yaml_path = Path(__file__).parent / "_tmp_permissions.yaml"
    yaml_path.write_text(
        "approval_ttl_seconds: 42\ntools:\n  预约挂号:\n    level: ask\n",
        encoding="utf-8",
    )
    try:
        engine = PermissionDecisionEngine.from_yaml(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)
    assert engine.approval_ttl_seconds == 42.0
    assert engine.policy_of("预约挂号") == PermissionLevel.ASK


def test_no_ui_policy_must_be_allow_or_deny():
    """no_ui_policy 拼错等于静默改安全策略 → 构造时直接报错"""
    with pytest.raises(ValueError):
        SecureToolExecutor(
            permission_engine=_engine(), no_ui_policy="Allow"  # 大小写敏感
        )
    with pytest.raises(ValueError):
        SecureToolExecutor(permission_engine=_engine(), no_ui_policy="ask")

    # 合法值正常构造
    SecureToolExecutor(permission_engine=_engine(), no_ui_policy="deny")
    SecureToolExecutor(permission_engine=_engine(), no_ui_policy="allow")


# ---- 按风险等级推导默认策略（核心模块第 2 批 T7） ----

from xiaozhi.core.tool import RiskLevel  # noqa: E402


def test_unregistered_tool_defaults_to_risk_level_policy():
    """未登记工具按 risk_level 推导：READ_ONLY→auto / WRITE→ask / DESTRUCTIVE→deny"""
    engine = PermissionDecisionEngine()
    engine.register_tool_default("新只读工具", RiskLevel.READ_ONLY)
    engine.register_tool_default("新写工具", RiskLevel.WRITE)
    engine.register_tool_default("危险工具2", RiskLevel.DESTRUCTIVE)

    assert engine.policy_of("新只读工具") == PermissionLevel.AUTO
    assert engine.policy_of("新写工具") == PermissionLevel.ASK
    assert engine.policy_of("危险工具2") == PermissionLevel.DENY


def test_explicit_yaml_policy_beats_risk_derivation():
    """YAML 显式配置永远优先，register_tool_default 不覆盖"""
    engine = PermissionDecisionEngine(policies={"预约挂号": PermissionLevel.DENY})
    engine.register_tool_default("预约挂号", RiskLevel.WRITE)
    assert engine.policy_of("预约挂号") == PermissionLevel.DENY
