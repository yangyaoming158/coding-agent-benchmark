"""GitHub 数据源：REST 和 GraphQL 两条路，都走 `gh` CLI（E8-T1，E1-T4 继续用）。

## 为什么走 `gh` 而不是自己拿 token 发 HTTP

token 不进仓库、不进 `.env`、不进环境变量，也不用管过期和刷新 —— 这些
`gh auth` 已经做完了。这个项目要接好几个服务商，凭据泄漏的风险本来就高
（`AGENTS.md` §7 把 `.env` 列进了绝不能提交的清单），少一处存 token 的地方
就少一处出事的地方。

代价是多起一个子进程，几十毫秒。对着一小时 5000 点的配额，这个代价可以忽略。

## 限流

认证用户 REST 5000 req/h、GraphQL 5000 点/h（§8.4）。GraphQL 的"点"不等于请求数：
一次请求要几点取决于它请求了多少个节点，所以每个查询都顺带把 `rateLimit`
带回来，调用方能看见自己烧了多少。

还有一层**次级限流**（短时间内请求太密），它不体现在配额里，表现是 403 加一句
"secondary rate limit"。这里遇到就退避重试，不把它当致命错误 —— 挖掘几十个仓库时
撞上它是常态，一撞就整个作业失败的话，跑一次要重来一次。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: 单次 `gh` 调用的墙钟上限。GraphQL 拉 100 个 PR 的文件列表偶尔要十几秒。
DEFAULT_TIMEOUT_S = 120

#: 撞上次级限流之后重试几次，以及每次等多久（秒）。
#: 等待时间翻倍：GitHub 的次级限流窗口是分钟级的，等 2 秒再撞一次没有意义。
MAX_RETRIES = 4
RETRY_BACKOFF_S = (5, 15, 45, 90)

#: 这些片段出现在 stderr 里就说明值得重试，而不是我们查询写错了。
_RETRYABLE = (
    "secondary rate limit",
    "abuse detection",
    "was submitted too quickly",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "timeout",
    "connection reset",
    # 网络层的偶发断开。这台开发机走代理连 GitHub 很不稳
    # （`AGENTS.md` §10；E2-T3 建镜像时 `git clone --mirror` 也反复被 reset）。
    # `gh` 用 Go 写的，代理半路掐断连接时报的原话是
    # `Post "https://api.github.com/graphql": EOF`（2026-09-09 实测，E1-T4 撞上）。
    # 匹配 `: eof` 连着冒号和空格，不匹配裸 `eof`：
    # 后者会误伤正文里恰好带这三个字母的正常错误信息。
    ": eof",
    "unexpected eof",
    "connection refused",
    "broken pipe",
    "tls handshake",
    "i/o timeout",
    # GitHub 自己那边的偶发失败，原话是
    # "Something went wrong while executing your query. Please include <id>..."。
    # 它**不是**我们查询写错了 —— 同一条查询隔几秒重发就过（2026-09-09 实测，
    # E1-T4 挖 pallets/click 时第一页就撞上一次，重发即成功）。
    # 不放进来的话，一次偶发就把整个仓库的挖掘作业打断。
    "something went wrong while executing your query",
)


class GitHubError(RuntimeError):
    """`gh` 调用失败，且不是可重试的那几种。"""


class GitHubNotFoundError(GitHubError):
    """仓库或对象不存在（404）。选型时这不是故障，是一条结论。"""


@dataclass(frozen=True, slots=True)
class RateBudget:
    """一次 GraphQL 查询报回来的配额情况。"""

    cost: int
    remaining: int
    limit: int


def gh_available() -> bool:
    """本机有没有装 `gh`。没有的话调用方应该跳过而不是报错。"""
    return shutil.which("gh") is not None


def _run(args: Sequence[str], *, timeout_s: int) -> str:
    """跑一次 `gh`，可重试的失败自动退避重试，返回 stdout。

    **超时也包成 `GitHubError`**，不让 `subprocess.TimeoutExpired` 冒出去：
    调用方（`probe_repo`）只 catch `GitHubError`，漏一种异常类型就等于
    "第 13 个仓库慢了一次，前 12 个的结果连同烧掉的配额一起没了"——
    2026-09-08 探大仓库时真这么翻过一次。
    """
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            completed = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # 超时按可重试处理：大仓库的 PR 查询偶尔就是慢，重试常常能过
            last = f"超过 {timeout_s} 秒没返回"
            if attempt >= MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
            logger.warning("gh 调用超时，退避重试", wait_s=wait, attempt=attempt + 1)
            time.sleep(wait)
            continue
        if completed.returncode == 0:
            return completed.stdout
        last = (completed.stderr or completed.stdout).strip()
        lowered = last.lower()
        if "not found" in lowered or "could not resolve to a repository" in lowered:
            raise GitHubNotFoundError(last[:400])
        if attempt >= MAX_RETRIES or not any(m.lower() in lowered for m in _RETRYABLE):
            break
        wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
        # 不写"撞上限流"：可重试的失败里限流只是一种，还有 GitHub 自己出错
        # 和代理掐断连接。写死成限流会把人往配额那边引（2026-09-09 实测，
        # 一条 `: EOF` 的代理断连被日志说成了限流）
        logger.warning("gh 调用失败，退避重试", wait_s=wait, attempt=attempt + 1, error=last[:200])
        time.sleep(wait)
    raise GitHubError(f"gh {' '.join(args[:2])} 失败：{last[:400]}")


def rest(path: str, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> Any:
    """调一次 REST，返回解析好的 JSON。`path` 形如 `repos/nonebot/nonebot2`。"""
    return json.loads(_run(["api", path], timeout_s=timeout_s))


def rest_text(path: str, *, accept: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    """调一次 REST，返回**原始文本**而不是解析好的 JSON。

    存在的理由只有一个：`Accept: application/vnd.github.v3.diff` 拿回来的是
    unified diff，不是 JSON。拿 `rest()` 去调会当场 `JSONDecodeError`，
    而错误信息完全看不出是媒体类型的问题（E1-T5 取 PR 补丁时要用）。
    """
    return _run(["api", "-H", f"Accept: {accept}", path], timeout_s=timeout_s)


def graphql(
    query: str,
    variables: Mapping[str, Any] | None = None,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[dict[str, Any], RateBudget | None]:
    """调一次 GraphQL，返回 `(data, 配额)`。

    查询里带上 `rateLimit { cost remaining limit }` 才拿得到第二个返回值 ——
    带不带由调用方决定，这里只负责如实转达。
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        # `-F` 会把数字和布尔当成对应类型传，`-f` 一律当字符串。
        # 游标、仓库名这些必须走 `-f`：一个纯数字的仓库名被当成 Int 传过去，
        # GraphQL 会报类型不匹配，而错误信息完全看不出是这个原因
        flag = "-F" if isinstance(value, bool | int) else "-f"
        args += [flag, f"{key}={value}"]

    payload = json.loads(_run(args, timeout_s=timeout_s))
    if payload.get("errors"):
        messages = "；".join(str(e.get("message", e)) for e in payload["errors"])
        raise GitHubError(f"GraphQL 报错：{messages[:400]}")

    data = payload.get("data") or {}
    limits = data.get("rateLimit")
    budget = (
        RateBudget(cost=limits["cost"], remaining=limits["remaining"], limit=limits["limit"])
        if limits
        else None
    )
    return data, budget


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "GitHubError",
    "GitHubNotFoundError",
    "RateBudget",
    "gh_available",
    "graphql",
    "rest",
    "rest_text",
]
