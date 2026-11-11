"""生产启动门禁（Phase 0/2 配置加固）单测。

两条红线：
  1. development 行为零变更——宽松默认值照常放行，259 个既有测试不受影响；
  2. production 聚合报全并拒绝启动，且必须发生在连接任何外部依赖之前。

``his_ready`` fixture 模拟「Phase 3b 已落地、真实 HIS 适配器已注册」，
用于把所有校验项逐个隔离测试；另有一条测试钉死当前代码事实：
真实适配器尚未注册时，production 恒定不可启动。
"""

from __future__ import annotations

import pytest

from xiaozhi import config_guard
from xiaozhi.config import Settings
from xiaozhi.config_guard import ProductionConfigError, validate_production_settings


def dev(**kwargs) -> Settings:
    kwargs.setdefault("app_env", "development")
    return Settings(_env_file=None, **kwargs)


def prod(**kwargs) -> Settings:
    kwargs.setdefault("app_env", "production")
    return Settings(_env_file=None, **kwargs)


@pytest.fixture
def his_ready(monkeypatch):
    """模拟 Phase 3b 完成：门禁承认 'his' 是已实现的真实适配器。"""
    monkeypatch.setattr(
        config_guard, "IMPLEMENTED_REAL_SLOT_ADAPTERS", frozenset({"his"})
    )
    return {
        "permission_no_ui": "deny",
        "mysql_password": "S3cure-Pass!",
        "slot_service_impl": "his",
        "auth_enabled": True,
        "auth_secret_key": "a-32-character-minimum-secret-key-1234567890",
        "auth_bootstrap_secret": "bootstrap-secret",
        "approval_enabled": True,
        # Phase 5（P1-01）：生产必须开启会话存储快速失败
        "storage_fail_fast": True,
        # Phase 6：审计必须 fail-closed 且集中存储；灌库必须移出 Web 进程
        "audit_fail_closed": True,
        "audit_sink": "file,mongo",
        "rag_ingest_on_startup": False,
        "rag_require_index": True,
        # Phase 7：医疗知识条目必须可溯源（来源/审核人/失效日期）
        "rag_require_doc_meta": True,
    }


# ---------------------------------------------------------------- development


def test_development_defaults_keep_permissive_values():
    """红线 1：默认值一个字都不能改，否则 259 个既有行为可能变。"""
    s = Settings(_env_file=None)
    assert s.app_env == "development"
    assert s.is_production is False
    assert s.permission_no_ui == "allow"
    assert s.mysql_password == "123456"
    assert s.slot_service_impl == "mock"
    assert s.profile_enabled is False


def test_development_never_validates():
    """开发模式下把所有坏配置叠满也必须照常放行。"""
    s = dev(
        permission_no_ui="allow",
        mysql_password="123456",
        profile_enabled=True,
        profile_secret_key="",
    )
    validate_production_settings(s)  # 不抛


@pytest.mark.parametrize("env", ["development", "DEVELOPMENT", " dev ", "local", ""])
def test_non_production_env_aliases_pass(env):
    validate_production_settings(dev(app_env=env))


# ---------------------------------------------------------------- production


@pytest.mark.parametrize("env", ["production", "PRODUCTION", "Production", " prod "])
def test_production_env_aliases_detected(env):
    s = dev(app_env=env)
    assert s.is_production is True
    with pytest.raises(ProductionConfigError):
        validate_production_settings(s)


def test_production_reports_all_problems_at_once():
    """默认配置下 11 项不合规（画像默认关，故不含密钥项），一次报全。"""
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod())
    msg = str(exc.value)
    assert "共 11 项不合规" in msg
    for token in (
        "permission_no_ui",
        "mysql_password",
        "slot_service_impl",
        "auth_enabled",
        "approval_enabled",
        "storage_fail_fast",
        "audit_fail_closed",
        "audit_sink",
        "rag_ingest_on_startup",
        "rag_require_index",
    ):
        assert token in msg
    assert "APP_ENV=development" in msg  # 错误信息要给出可执行的修复方向


def test_production_passes_when_all_compliant(his_ready):
    """HIS 落地后，全合规配置应当允许启动。"""
    validate_production_settings(prod(**his_ready))


def test_production_rejects_no_ui_allow(his_ready):
    """最大单点：无审批 UI 时放行 ASK 级写操作 == 无确认挂号。"""
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(
            prod(**{**his_ready, "permission_no_ui": "allow"})
        )
    assert "permission_no_ui" in str(exc.value)
    assert "deny" in str(exc.value)


@pytest.mark.parametrize(
    "pwd",
    [
        "",
        "123456",
        "12345678",
        "123456789",
        "root",
        "ROOT",
        "password",
        "admin",
        "mysql",
        "test",
        " 12345678 ",
    ],
)
def test_production_rejects_weak_passwords(his_ready, pwd):
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "mysql_password": pwd}))
    assert "mysql_password" in str(exc.value)


def test_production_rejects_profile_without_key(his_ready):
    """画像缺密钥在开发下静默禁用；生产不允许这种降级。"""
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(
            prod(**{**his_ready, "profile_enabled": True, "profile_secret_key": ""})
        )
    assert "profile_secret_key" in str(exc.value)


def test_production_accepts_profile_with_key(his_ready):
    validate_production_settings(
        prod(**{**his_ready, "profile_enabled": True, "profile_secret_key": "fernet-key"})
    )


def test_production_rejects_mock_slot_service(monkeypatch):
    monkeypatch.setattr(
        config_guard, "IMPLEMENTED_REAL_SLOT_ADAPTERS", frozenset({"his"})
    )
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(
            prod(
                permission_no_ui="deny",
                mysql_password="S3cure-Pass!",
                slot_service_impl="mock",
            )
        )
    assert "slot_service_impl" in str(exc.value)
    assert "MockSlotService" in str(exc.value)


def test_no_real_his_adapter_registered_yet():
    """钉死当前事实：Phase 3b 之前，production 恒定不可启动。

    防止有人把 APP_ENV=production 误当成「已可上线」的状态对外宣称。
    """
    assert config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS == frozenset()
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(
            prod(
                permission_no_ui="deny",
                mysql_password="S3cure-Pass!",
                slot_service_impl="his",
            )
        )
    assert "HIS" in str(exc.value)


# ---------------------------------------------------------------- 组合根接线


@pytest.mark.asyncio
async def test_get_application_fails_before_external_calls(monkeypatch):
    """门禁必须在 RAG 灌库 / 连接 Ollama、Milvus 之前触发。"""
    from xiaozhi import application

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("门禁未生效：已经开始连接外部依赖")

    monkeypatch.setattr(application.RagService, "bootstrap", _forbidden)
    with pytest.raises(ProductionConfigError) as exc:
        await application.get_application(prod())
    assert "APP_ENV=production" in str(exc.value)


def test_build_slot_service_returns_mock_in_development():
    from xiaozhi.application import _build_slot_service
    from xiaozhi.service.slot_service import MockSlotService

    assert isinstance(_build_slot_service(dev(), object()), MockSlotService)


def test_build_slot_service_rejects_unregistered_adapter():
    """填了未实现的适配器名要明确报错，不能悄悄退回 Mock。"""
    from xiaozhi.application import _build_slot_service
    from xiaozhi.config_guard import ProductionConfigError

    with pytest.raises(ProductionConfigError) as exc:
        _build_slot_service(dev(slot_service_impl="not_exist"), object())
    assert "尚未实现" in str(exc.value)


def test_build_slot_service_builds_his_adapter_when_configured():
    """Phase 3b：``his`` 分支能装配出真实适配器（配置齐全时）。"""
    from xiaozhi.application import _build_slot_service
    from xiaozhi.service.his_slot_service import HisSlotService

    settings = dev(
        slot_service_impl="his",
        his_api_base_url="http://his-sandbox.example.internal",
        his_api_token="sandbox-token",
    )
    service = _build_slot_service(settings, object())
    assert isinstance(service, HisSlotService)


def test_build_slot_service_his_requires_base_url():
    """选了 his 却没有网关地址 → 明确报错（不能静默退回 Mock）。"""
    from xiaozhi.application import _build_slot_service
    from xiaozhi.config_guard import ProductionConfigError

    with pytest.raises(ProductionConfigError) as exc:
        _build_slot_service(dev(slot_service_impl="his"), object())
    assert "HIS_API_BASE_URL" in str(exc.value)


def test_his_adapter_not_yet_released_to_production():
    """钉死当前事实：适配层写完 ≠ 真实挂号可用。

    ``IMPLEMENTED_REAL_SLOT_ADAPTERS`` 仍为空 —— 只有完成真实沙箱端到端
    验收后，才允许把 "his" 登记进去。防止有人看到代码就宣称"已可上线"。
    """
    assert config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS == frozenset()
    assert "his" not in config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS


# ---------------------------------------------------------------- 认证门禁（P1-A）


def test_development_auth_defaults_off():
    """红线：认证默认关，否则 291 个既有行为与前端协议都会变。"""
    s = Settings(_env_file=None)
    assert s.auth_enabled is False
    assert s.auth_secret_key == ""
    assert s.auth_bootstrap_secret == ""
    assert s.auth_ops_subjects == []


def test_production_rejects_auth_disabled(his_ready):
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "auth_enabled": False}))
    assert "auth_enabled" in str(exc.value)


def test_production_rejects_short_auth_key(his_ready):
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "auth_secret_key": "short"}))
    assert "auth_secret_key" in str(exc.value)


def test_production_rejects_missing_bootstrap_secret(his_ready):
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "auth_bootstrap_secret": ""}))
    assert "auth_bootstrap_secret" in str(exc.value)


# ---------------------------------------------------------------- 审批门禁（P2-C）


def test_development_approval_defaults_off():
    """红线：审批默认关，SecureToolExecutor 行为与一期逐字一致。"""
    s = Settings(_env_file=None)
    assert s.approval_enabled is False
    assert s.approval_ttl_seconds == 120.0


def test_production_rejects_approval_disabled(his_ready):
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "approval_enabled": False}))
    assert "approval_enabled" in str(exc.value)
    assert "一次性 token" in str(exc.value)


# ---------------------------------------------------------------- 存储门禁（P5 / P1-01）


def test_development_storage_fail_fast_defaults_off():
    """红线：默认仍走 Resilient 降级（内网演示不受影响）。"""
    s = Settings(_env_file=None)
    assert s.storage_fail_fast is False


def test_production_rejects_storage_degradation(his_ready):
    """生产禁止把 Mongo 故障静默降级成本地文件（会产生第二个数据源）。"""
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod(**{**his_ready, "storage_fail_fast": False}))
    assert "storage_fail_fast" in str(exc.value)
    assert "本地文件" in str(exc.value)
