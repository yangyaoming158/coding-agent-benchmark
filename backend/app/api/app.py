"""FastAPI 应用装配。

目前只有健康检查。完整的 REST 接口是 E5 的活，这里先把骨架和启动方式定下来，
让 `make dev` 有东西可起 —— 前端脚手架要对着一个真实的服务调通才有意义。
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from app.api import health
from app.domain.protocol import PROTOCOL_VERSION
from app.infrastructure.db import create_db_engine

#: 开发时允许跨域访问的前端地址，逗号分隔。
DEV_FRONTEND_ORIGINS = os.environ.get(
    "BENCH_DEV_FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """进程内共用一个引擎。

    缓存起来是必要的：每个请求新建引擎会各自带一个连接池，
    几十个请求之后数据库连接就耗尽了。
    """
    return create_db_engine()


def create_app() -> FastAPI:
    """建应用。写成工厂函数而不是模块级单例，测试里才能建互不干扰的实例。"""
    app = FastAPI(
        title="AI Coding Agent 评测基准平台",
        version=PROTOCOL_VERSION,
        description=(
            "把一个开源项目里已经修好的真 bug 回退到修复前，把当初那份 issue "
            "交给被测 AI，再用项目自己的测试验证它交出来的补丁。"
        ),
    )
    # 开发时前端在 3000、后端在 8000，浏览器会把跨端口请求当跨域拦下来。
    # 只放行本机的前端地址，不写 "*" —— 生产环境前后端同域，这段配置不该起作用。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in DEV_FRONTEND_ORIGINS.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    return app
