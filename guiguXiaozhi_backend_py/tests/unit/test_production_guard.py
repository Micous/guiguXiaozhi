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
    """默认配置下 3 项不合规（画像默认关，故不含密钥项），一次报全。"""
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(prod())
    msg = str(exc.value)
    assert "共 3 项不合规" in msg
    for token in ("permission_no_ui", "mysql_password", "slot_service_impl"):
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

    with pytest.raises(ProductionConfigError) as exc:
        _build_slot_service(dev(slot_service_impl="his"), object())
    assert "尚未实现" in str(exc.value)
