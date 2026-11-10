"""集成测试夹具：连接一套**已经在运行**的 Python 后端。

    XIAOZHI_PY_BASE_URL=http://127.0.0.1:8082 \
      ./.venv/bin/python -m pytest tests/integration -q --run-integration

服务不可用时整体 skip，不会把「没起服务」误报成代码失败。
"""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.getenv("XIAOZHI_PY_BASE_URL", "http://127.0.0.1:8081").rstrip("/")


@pytest.fixture(scope="session")
def live_client(base_url: str):
    # trust_env=False：绕开机器上的全局 HTTP 代理，直连 localhost
    try:
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as probe:
            resp = probe.get("/health")
            if resp.status_code != 200:
                pytest.skip(f"{base_url}/health 返回 {resp.status_code}")
            # 版本守卫：改动前的旧进程没有 /metrics，不要把它误判成新版后端
            metrics = probe.get("/metrics")
            if metrics.status_code != 200:
                pytest.fail(
                    f"{base_url} 不是新版 Python 后端（/metrics 返回 "
                    f"{metrics.status_code}）。8081 上可能仍是改动前的旧进程；"
                    "请用 `run.py --port 8082` 启动新代码并设置 "
                    "XIAOZHI_PY_BASE_URL=http://127.0.0.1:8082。"
                )
    except httpx.HTTPError as exc:
        pytest.skip(f"集成测试服务不可用（{base_url}）：{type(exc).__name__}: {exc}")

    client = httpx.Client(base_url=base_url, timeout=180.0, trust_env=False)
    yield client
    client.close()
