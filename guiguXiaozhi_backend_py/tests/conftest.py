"""pytest 全局钩子。

离线单测是默认路径；集成测试打真实 Ollama / Milvus / MySQL / MongoDB，
只有显式加 ``--run-integration`` 才会执行，避免 CI 或本地快速回归被环境卡住。
"""

from __future__ import annotations

import pytest


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
