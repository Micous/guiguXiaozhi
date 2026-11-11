"""pytest 全局钩子。

离线单测是默认路径；集成测试打真实 Ollama / Milvus / MySQL / MongoDB，
只有显式加 ``--run-integration`` 才会执行，避免 CI 或本地快速回归被环境卡住。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_local_env(monkeypatch):
    """离线单测必须不受仓库 ``.env`` 的本地运行配置影响。

    2026-11-11：本机为「默认登录页」在 ``guiguXiaozhi_backend_py/.env`` 里打开了
    AUTH_ENABLED，导致 ``test_docs_available_in_development`` 这类断言默认态的
    用例误读到认证开启。环境变量优先级高于 .env，这里统一把灰度开关钉回关闭态；
    单个用例仍可自行 ``monkeypatch.setenv`` 覆盖（LIFO 撤销不会串味）。
    """
    from xiaozhi.config import reset_settings

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("PROFILE_ENABLED", "false")
    reset_settings()
    yield
    reset_settings()


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行 tests/integration/ 中依赖真实服务的集成测试",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="需要真实依赖；使用 pytest --run-integration 显式运行"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
