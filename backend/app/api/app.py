"""FastAPI 应用装配。

路由按资源分文件（E7-T0）：

| 文件 | 端点 |
|:---|:---|
| `health` | `/api/health` |
| `benchmark_sets` | `/api/benchmark-sets{,/{slug}}` |
| `tasks` | `/api/tasks{,/{task_id}}` |
| `agents` | `/api/agents`、`/api/agent-configs` |
| `runs` | `/api/runs{,/{id}}`、`/cancel`、`/retry-failed`、`/task-runs` |
| `task_runs` | `/api/task-runs/{id}{,/tests,/artifacts/{kind}}` |
| `leaderboard` | `/api/leaderboard` |
| `analysis` | `/api/analysis` |

**没配 `ADMIN_TOKEN` 就起不来**（E7-T0 AC-3）。写操作靠这个 token 把门，
默认放行的部署从外面看和配好了的一模一样 —— 没有任何症状，
直到有人发现谁都能掐掉一场跑了两小时的实验。起不来是看得见的。
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import (
    agents,
    analysis,
    benchmark_sets,
    health,
    leaderboard,
    reports,
    reviews,
    runs,
    task_runs,
    tasks,
)
from app.api.deps import require_admin_token_configured
from app.api.errors import install_error_handlers
from app.domain.protocol import PROTOCOL_VERSION
from app.infrastructure.config import get_settings
from app.infrastructure.db import create_db_engine, create_session_factory
from app.infrastructure.logging import configure_logging, get_logger


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """进程内共用一个引擎。

    缓存起来是必要的：每个请求新建引擎会各自带一个连接池，
    几十个请求之后数据库连接就耗尽了。
    """
    return create_db_engine()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """进程内共用一个会话工厂。请求级的会话由 `app.api.deps.get_session` 开。"""
    return create_session_factory(get_engine())


def create_app() -> FastAPI:
    """建应用。写成工厂函数而不是模块级单例，测试里才能建互不干扰的实例。"""
    settings = get_settings()
    # 日志在这里配一次。Worker 有自己的入口，也要各配一次 ——
    # 两个进程各配各的，不共享。
    configure_logging(settings)
    # 没配 ADMIN_TOKEN 直接抛，进程起不来（AC-3）
    require_admin_token_configured()

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
        allow_origins=settings.frontend_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 错误响应统一成 {code, message}（AC-8）。要在挂路由之前装，
    # 不然 Starlette 自己的 404 处理器会先注册上去
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(benchmark_sets.router)
    app.include_router(tasks.router)
    app.include_router(agents.router)
    app.include_router(runs.router)
    app.include_router(task_runs.router)
    app.include_router(leaderboard.router)
    app.include_router(analysis.router)
    app.include_router(reviews.router)
    app.include_router(reports.router)

    get_logger(__name__).info(
        "API 已装配",
        protocol_version=PROTOCOL_VERSION,
        artifact_backend=settings.artifact_backend,
        cors_origins=settings.frontend_origins,
    )
    return app
