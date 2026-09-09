"""GitHub 响应的本地缓存（E1-T4，`03-benchmark-spec.md` §8.4）。

一句话：**同一个查询问第二遍不再走网络，所以挖掘可以随便重跑。**

存取的机制在 `app.infrastructure.file_cache`（E1-T5 起和大模型缓存共用一份）。
这里只管两件 GitHub 特有的事：key 里放什么、命中之后配额怎么报。

## 为什么不是 `gh_cache` 表

§8.4 写的是"本地缓存（`gh_cache` 表按 URL+etag）"。这里落地成文件缓存，
除了 `file_cache` 模块文档里那三条通用理由，还有一条是 GitHub 独有的：

**GraphQL 没有 ETag。** ETag 是 HTTP GET 的条件请求机制，GitHub 只在 REST 上给。
GraphQL 全部走 POST 打同一个 URL（`/graphql`），没有 ETag 可存，也没有
"304 不计配额"这回事。而挖掘的配额几乎全烧在 GraphQL 上 ——
按 URL+etag 建的表，对真正花钱的那条路一点用没有。

## TTL

默认 7 天。挖的是**已合并**的 PR 和**已关闭**的 issue，正文和文件列表基本不再变；
真变了（有人事后编辑 issue）也不影响判定，因为题目一旦验过就冻结在
`benchmark_tasks` 里了。要强制重拉用 `--refresh`。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.benchmark.github import DEFAULT_TIMEOUT_S, RateBudget, graphql, rest, rest_text
from app.infrastructure import file_cache
from app.infrastructure.file_cache import CACHE_ROOT, DEFAULT_TTL_S, CacheStats

#: 缓存根目录。GitHub 的东西单独一个命名空间，和大模型缓存分开，
#: 这样"清掉 GitHub 缓存重拉一遍"不会连带把花过钱的大模型回答一起删掉。
DEFAULT_CACHE_ROOT = CACHE_ROOT
NAMESPACE = "github"


def cache_key(kind: str, query: str, variables: Mapping[str, Any] | None = None) -> str:
    """`(查询类型, 查询串, 变量)` → 64 位十六进制。

    **查询串整个进哈希**：改了查询就该重新拉，拿旧结果套新字段会静默少数据。
    E1-T4 给 `parents` 加 `associatedPullRequests` 那次就靠这条自动失效。
    """
    return file_cache.cache_key(f"gh:{kind}", {"query": query, "variables": dict(variables or {})})


def cache_path(key: str, *, root: Path | None = None) -> Path:
    return file_cache.cache_path(key, root=root or DEFAULT_CACHE_ROOT, namespace=NAMESPACE)


def read(key: str, *, ttl_s: int = DEFAULT_TTL_S, root: Path | None = None) -> Any | None:
    return file_cache.read(key, root=root or DEFAULT_CACHE_ROOT, namespace=NAMESPACE, ttl_s=ttl_s)


def write(
    key: str,
    data: Any,
    *,
    root: Path | None = None,
    meta: Mapping[str, Any] | None = None,
) -> None:
    file_cache.write(key, data, root=root or DEFAULT_CACHE_ROOT, namespace=NAMESPACE, meta=meta)


def cache_size(root: Path | None = None) -> tuple[int, int]:
    return file_cache.cache_size(root or DEFAULT_CACHE_ROOT, namespace=NAMESPACE)


def cached_graphql(
    query: str,
    variables: Mapping[str, Any] | None = None,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    ttl_s: int = DEFAULT_TTL_S,
    root: Path | None = None,
    refresh: bool = False,
    stats: CacheStats | None = None,
) -> tuple[dict[str, Any], RateBudget | None, bool]:
    """带缓存的 GraphQL，返回 `(data, 配额, 是否命中缓存)`。

    命中时**配额返回 `None`** —— 没发请求就没有配额消耗，
    编一个 0 出来会让"这次挖掘烧了多少点"这个数字失真。

    `ttl_s < 0` 表示不缓存（`--no-cache`），直接透传。
    """
    if ttl_s < 0:
        data, budget = graphql(query, variables, timeout_s=timeout_s)
        return data, budget, False

    key = cache_key("graphql", query, variables)
    if not refresh:
        hit = read(key, ttl_s=ttl_s, root=root)
        if hit is not None:
            if stats:
                stats.hits += 1
            return dict(hit), None, True

    data, budget = graphql(query, variables, timeout_s=timeout_s)
    write(key, data, root=root, meta={"kind": "graphql", "variables": dict(variables or {})})
    if stats:
        stats.misses += 1
    return data, budget, False


def cached_rest(
    path: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    ttl_s: int = DEFAULT_TTL_S,
    root: Path | None = None,
    refresh: bool = False,
    stats: CacheStats | None = None,
) -> Any:
    """带缓存的 REST。挖掘用它取仓库元数据；E1-T5 用它取 PR 的 diff。"""
    if ttl_s < 0:
        return rest(path, timeout_s=timeout_s)

    key = cache_key("rest", path)
    if not refresh:
        hit = read(key, ttl_s=ttl_s, root=root)
        if hit is not None:
            if stats:
                stats.hits += 1
            return hit

    data = rest(path, timeout_s=timeout_s)
    write(key, data, root=root, meta={"kind": "rest", "path": path})
    if stats:
        stats.misses += 1
    return data


def cached_rest_text(
    path: str,
    *,
    accept: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    ttl_s: int = DEFAULT_TTL_S,
    root: Path | None = None,
    refresh: bool = False,
    stats: CacheStats | None = None,
) -> str:
    """带缓存的原始文本 REST。E1-T5 取 PR 的 diff 走这条。

    `accept` 进缓存 key：同一个 `repos/X/pulls/N`，要 JSON 和要 diff
    拿回来的是完全不同的东西，共用一个 key 会串味。
    """
    if ttl_s < 0:
        return rest_text(path, accept=accept, timeout_s=timeout_s)

    key = file_cache.cache_key("gh:rest_text", {"path": path, "accept": accept})
    if not refresh:
        hit = read(key, ttl_s=ttl_s, root=root)
        if hit is not None:
            if stats:
                stats.hits += 1
            return str(hit)

    data = rest_text(path, accept=accept, timeout_s=timeout_s)
    write(key, data, root=root, meta={"kind": "rest_text", "path": path, "accept": accept})
    if stats:
        stats.misses += 1
    return data


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "DEFAULT_TTL_S",
    "NAMESPACE",
    "CacheStats",
    "cache_key",
    "cache_path",
    "cache_size",
    "cached_graphql",
    "cached_rest",
    "cached_rest_text",
    "read",
    "write",
]
