"""按内容寻址的本地文件缓存（E1-T4 起用，E1-T5 起共用）。

一句话：**同一个问题问第二遍不再走网络。**

两个地方要它，而且要的东西一模一样：

- `app.benchmark.gh_cache`：GitHub 的 GraphQL / REST 响应（E1-T4）
- `app.infrastructure.llm`：大模型的回答（E1-T5 预筛、E6-T2 归因）

两者的共同点是**贵**：一个烧 API 配额，一个真花钱。而它们缓存的东西都满足
"同样的输入必然得到同样的输出，或者输出变了也不影响结论"。

## 为什么放 `infrastructure` 而不是各写一份

`app.infrastructure` 是分层里最底下能被所有人 import 的一层。放高了不行 ——
`app.attribution`（E6-T2 要用）在 `app.benchmark` **下面**一层，
缓存放 benchmark 里的话 attribution 根本够不着，最后只能再抄一份。
两份缓存会各自长出自己的 TTL 语义和坏文件处理，而这两件事都很容易写错。

## 为什么不用数据库

三条，逐条都成立：

1. **`make check` 会清空开发库**（`downgrade base` + `upgrade head`）。
   缓存的全部价值就是"重跑不再花钱"，跟着 check 一起被清掉等于白建。
2. **它不是评测数据。** 没有外键、不进 API、不上前端、没人 SQL 查它。
3. 建表要写迁移 + 回滚 + 模型 + 测试，换来的是"用缓存之前必须先起 Postgres"。

代价：缓存是**本机**的，换台机器要重新花钱。只有一台开发机，可接受。

## key 怎么算

`sha256(kind + 载荷的规范 JSON)`，`sort_keys` 序列化 —— dict 的字面顺序不该影响命中。
载荷里要放**所有会影响答案的东西**：查询串、变量、模型名、温度、prompt 版本。
漏放一个，改了参数还会读到旧答案，而且不报错。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.infrastructure.config import REPO_ROOT
from app.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: 所有文件缓存的根。在 `.gitignore` 的 `var/` 下，不进版本库。
CACHE_ROOT = REPO_ROOT / "var" / "cache"

#: 不给 TTL 时的默认过期时间：7 天。
DEFAULT_TTL_S = 7 * 24 * 3600

#: `ttl_s` 传这个（或任何负数）表示**完全不用缓存**：既不读也不写。
NO_CACHE = -1


@dataclass
class CacheStats:
    """一轮作业里缓存的命中情况，进报表用。"""

    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.total if self.total else None

    def to_json(self) -> dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": self.hit_rate}


def cache_key(kind: str, payload: Mapping[str, Any]) -> str:
    """`(种类, 载荷)` → 64 位十六进制。

    `kind` 进哈希而不是只当目录名：`{"q": "x"}` 在 GitHub 查询和大模型 prompt 里
    含义完全不同，让它们有机会撞上同一个 key 没有任何好处。
    """
    blob = json.dumps(
        {"kind": kind, "payload": dict(payload)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_path(key: str, *, root: Path, namespace: str = "") -> Path:
    """key → 文件路径。按前两位分子目录，避免一个目录里堆几万个文件。"""
    base = root / namespace if namespace else root
    return base / key[:2] / f"{key}.json"


def read(key: str, *, root: Path, namespace: str = "", ttl_s: int = DEFAULT_TTL_S) -> Any | None:
    """读缓存。没有、过期、或者文件坏了都返回 `None`（一律当没命中）。

    坏文件不抛异常：缓存是纯优化，为它中断一次作业不值得 —— 大不了重新问一次，
    而抛出去会让这一轮前面花掉的配额和钱一起白费。
    """
    path = cache_path(key, root=root, namespace=namespace)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("缓存文件读不出来，当成没命中", path=str(path))
        return None
    if ttl_s >= 0 and time.time() - float(record.get("fetched_at", 0)) > ttl_s:
        return None
    return record.get("data")


def write(
    key: str,
    data: Any,
    *,
    root: Path,
    namespace: str = "",
    meta: Mapping[str, Any] | None = None,
) -> None:
    """写缓存。写不进去只记一条日志，不影响作业本身。

    先写临时文件再 `replace`：作业中途被 Ctrl-C 打断时，半截 JSON 留在缓存里
    会让下一次重跑读到坏数据。
    """
    path = cache_path(key, root=root, namespace=namespace)
    record = {"fetched_at": time.time(), "meta": dict(meta or {}), "data": data}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("缓存写不进去，跳过", path=str(path), error=str(exc))


def cache_size(root: Path, *, namespace: str = "") -> tuple[int, int]:
    """`(文件数, 总字节数)`。给 CLI 打一行"缓存现在多大"用。"""
    base = root / namespace if namespace else root
    if not base.exists():
        return 0, 0
    files = list(base.rglob("*.json"))
    return len(files), sum(f.stat().st_size for f in files)


__all__ = [
    "CACHE_ROOT",
    "DEFAULT_TTL_S",
    "NO_CACHE",
    "CacheStats",
    "cache_key",
    "cache_path",
    "cache_size",
    "read",
    "write",
]
