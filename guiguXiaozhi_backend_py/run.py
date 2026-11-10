"""启动入口。

    python run.py              # 默认 127.0.0.1:8081
    python run.py --port 8082
"""

from __future__ import annotations

import argparse

import uvicorn

from xiaozhi.config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="硅谷小智 Python 后端")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    uvicorn.run(
        "xiaozhi.server.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
