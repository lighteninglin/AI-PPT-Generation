#!/usr/bin/env python3
"""
PPT-Web 后端启动脚本

用法:
    python run.py
    python run.py --port 8080
"""

import argparse
import uvicorn


def main() -> None:
    """启动 FastAPI 服务"""
    parser = argparse.ArgumentParser(description="PPT-Web 后端服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    parser.add_argument("--no-reload", action="store_true", help="禁用热重载")
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
    )


if __name__ == "__main__":
    main()
