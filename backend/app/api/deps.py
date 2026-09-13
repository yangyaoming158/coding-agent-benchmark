"""接口层的依赖：数据库会话、分页、写操作鉴权、可复现性凭证（E7-T0）。

## 鉴权：单一管理员 Token（§14.4）

写操作要带 `X-Bench-Token`，读接口开放。P0 不做用户体系（那是 P2，§29）。

**没配 token 就拒绝启动**（AC-3）。为什么不是"没配就放行"：
没配就放行的部署，从外面看和配好了的一模一样 —— 没有任何症状，
直到有人发现谁都能 `POST /api/runs/{id}/cancel` 掐掉一场跑了两小时的实验。
起不来是看得见的，误放行是看不见的。

## 分页：limit + offset，稳定排序（AC-4）

所有列表端点共用 `PageParams`。排序键必须**唯一**（一般就是 id），
否则同一个 offset 翻两次可能给出不同的行：Postgres 对非唯一排序键
不保证稳定顺序，行一多、执行计划一变就会重复或漏行，而且极难查
—— 前端看到的是"第 2 页有一行和第 1 页重复"，后端日志里什么都没有。
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.errors import forbidden
from app.evaluation.manifest import collect_provenance
from app.infrastructure.config import get_settings

#: 列表端点默认返回多少行。
DEFAULT_LIMIT = 50
#: 一次最多返回多少行。挡的是 `?limit=100000` 那种把整库拖进内存的请求。
MAX_LIMIT = 200

#: 写操作的认证头（§14.4）。
TOKEN_HEADER = "X-Bench-Token"


# ── 数据库会话 ──────────────────────────────────────────────


def get_session() -> Iterator[Session]:
    """一个请求一个会话，请求结束就关掉。

    不在这里 commit：读接口没什么可提交的，写接口自己在路由里 commit ——
    那样"哪一步之后才算落库"看得见，而不是藏在依赖的收尾逻辑里。
    """
    from app.api.app import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


# ── 分页 ────────────────────────────────────────────────────


class PageParams(BaseModel):
    """列表端点的分页参数。"""

    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="最多返回多少行")
    offset: int = Field(0, ge=0, description="跳过多少行")


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="最多返回多少行")] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="跳过多少行")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    """分页后的一批数据。

    `total` 是过滤之后、分页之前的总行数 —— 前端要靠它算页数。
    多一次 `count(*)` 查询，这些表最大也就几万行，值得。
    """

    items: list[ItemT]
    total: int
    limit: int
    offset: int


def page_of(items: list[ItemT], *, total: int, params: PageParams) -> Page[ItemT]:
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


# ── 写操作鉴权 ──────────────────────────────────────────────


def require_admin_token(
    x_bench_token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> None:
    """写操作的门卫。token 不对就 403。

    比对用 `hmac.compare_digest` 而不是 `==`：后者是逐字节短路比较，
    比对耗时会泄漏"前几位对了几位"。这不是理论风险，是写认证代码的常规做法，
    而且这里一行就能做到。

    配置缺失在这里抛的是 500 而不是 403 —— 那是部署的问题不是调用方的问题。
    正常情况下进程根本起不来（见 `require_admin_token_configured`），
    这一条是兜底，防的是有人绕过工厂函数直接建 app。
    """
    expected = get_settings().admin_token
    if expected is None:  # pragma: no cover - 启动时就该拦住
        raise RuntimeError(f"没配 ADMIN_TOKEN，写接口不能用（{TOKEN_HEADER}）")
    if x_bench_token is None:
        raise forbidden("MISSING_TOKEN", f"写操作要带 {TOKEN_HEADER} 请求头")
    if not hmac.compare_digest(x_bench_token, expected.get_secret_value()):
        raise forbidden("INVALID_TOKEN", f"{TOKEN_HEADER} 不对")


#: 挂在写路由上：`dependencies=[AdminTokenDep]`。
AdminTokenDep = Depends(require_admin_token)


def require_admin_token_configured() -> None:
    """启动时检查 `ADMIN_TOKEN` 配了没有。没配就**拒绝启动**（AC-3）。

    在 `create_app()` 里调，所以问题在 `make dev-api` 的第一秒就暴露，
    而不是等某天有人发现写接口没人把门。
    """
    if get_settings().admin_token is None:
        raise RuntimeError(
            "没配 ADMIN_TOKEN，拒绝启动。\n"
            f"  写接口（建实验、取消、补跑）靠 {TOKEN_HEADER} 这个头保护，"
            "没有它就等于谁都能掐掉一场正在跑的实验。\n"
            "  在 backend/.env 里加一行：ADMIN_TOKEN=<随便一串足够长的随机字符>\n"
            '  生成一个：python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )


# ── 可复现性凭证 ────────────────────────────────────────────


def get_provenance_collector():  # type: ignore[no-untyped-def]
    """建实验时用哪个函数去凑可复现性事实。生产路径永远是 `collect_provenance`。

    做成依赖是为了让集成测试能换掉它。**不是为了绕过 C-27，恰恰相反**：
    `collect_provenance()` 里会调 `git status`，而开发时工作区永远是脏的，
    不换掉的话每个建实验的测试都会因为"工作区不干净"变红 ——
    而那和被测的东西（路由把参数转给编排层没转错）一点关系都没有。

    同一个理由已经写在 `app.evaluation.manifest` 的模块文档里：
    git 在 `collect_provenance()` 里调，`create_runs()` 只收一个必填的凭证。
    这里是那条分界线在 HTTP 这一侧的延长。

    有一条测试钉住"默认拿到的就是 `collect_provenance` 本人"
    （`tests/integration/test_api_runs.py`），换掉它不会悄悄成为默认行为。
    """
    return collect_provenance


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "TOKEN_HEADER",
    "AdminTokenDep",
    "Page",
    "PageDep",
    "PageParams",
    "SessionDep",
    "get_provenance_collector",
    "get_session",
    "page_of",
    "page_params",
    "require_admin_token",
    "require_admin_token_configured",
]
