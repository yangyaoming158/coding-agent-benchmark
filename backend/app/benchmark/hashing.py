"""题目内容哈希（`content_hash`）。

它的作用是让"数据集版本"成为一个**可验证的事实**，而不是靠"我记得当时是这样"。
数据集发布时，`benchmark_set_items` 冻结每道题当时的 `content_hash`；三周后重跑，
比一下哈希就知道题目有没有被动过（§7.5、NFR-02）。

## 哈希哪些字段：除了两个，全都哈希

排除项只有两个：

- `content_hash` 自己——它是哈希的结果，不能是输入。
- `validation`——记的是"这题被验证过"的过程结果，不是题目内容。数据集发布后
  每周会复验一次，`validated_at` 和 `image_digest` 都会变，但题目本身没变。
  把它算进去的话，每复验一次全部数据集快照就集体失配。

**为什么不手工列一份"判定相关字段"清单**：清单会烂。以后有人给 Schema 加字段，
忘了往清单里加一条，哈希就悄悄不覆盖那个字段了，而且没有任何报错。
"除了这两个全都算"的规则相反——新字段默认被覆盖，漏掉的成本是"哈希变多了"
（顶多误报一次"题目变了"），不是"哈希漏了"（漏报等于可复现性是假的）。

## 规范化

哈希前把对象转成**规范 JSON**：递归按键名排序、不留空格、不转义非 ASCII。
所以字段写的顺序不影响结果——同样的内容换个字段序，哈希一样。

列表**不排序**。`fail_to_pass` 这些集合语义的列表是在 `TaskDefinition` 解析时
就排好序去过重的（见 schema.py），规范化只是把已经整理好的数据序列化一遍。
把排序藏在哈希里会有个坏处：将来加一个顺序有意义的列表字段，
哈希会悄悄把顺序抹平，而数据本身没变。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: 不参与哈希的字段，理由见模块文档。
EXCLUDED_FIELDS = frozenset({"content_hash", "validation"})

#: 哈希值的前缀。§7.1 的任务 JSON 里写的是 `"content_hash": "sha256:..."`。
HASH_PREFIX = "sha256:"


def canonical_json(payload: Mapping[str, Any]) -> str:
    """转成规范 JSON 文本：键名排序、无多余空格、原样保留中文。

    `ensure_ascii=False` 很重要：题目里大量中文，转义成 `\\uXXXX` 之后哈希照样稳定，
    但人就没法直接看规范化的结果对不对了，出问题时排查成本高很多。
    """
    trimmed = {key: value for key, value in payload.items() if key not in EXCLUDED_FIELDS}
    return json.dumps(trimmed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_content_hash(payload: Mapping[str, Any]) -> str:
    """算题目内容哈希，返回 `sha256:` 开头的字符串。"""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def to_bare_hex(content_hash: str) -> str:
    """去掉 `sha256:` 前缀，取 64 位裸十六进制。

    数据库那一列是 `CHAR(64)`（`benchmark_tasks.content_hash`），装不下带前缀的
    71 个字符；而 §7.1 的任务 JSON 里带前缀。两边格式不同是既定事实，
    在这里转一次，别让每个调用点各写一遍字符串切片。
    """
    return content_hash.removeprefix(HASH_PREFIX)


__all__ = [
    "EXCLUDED_FIELDS",
    "HASH_PREFIX",
    "canonical_json",
    "compute_content_hash",
    "to_bare_hex",
]
