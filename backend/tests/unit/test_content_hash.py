"""`content_hash` 的测试（E1-T1 的验收标准之一）。

哈希的用途是让"数据集版本"成为可验证的事实：发布时把每道题的哈希冻进
`benchmark_set_items`，三周后重跑比一下就知道题目有没有被动过（§7.5、NFR-02）。

所以它要满足两件互相拉扯的事：

- **同样的内容 → 同样的哈希**，字段写的顺序、列表元素的顺序都不能影响它；
- **内容变了 → 哈希必须变**，不能有哪个字段悄悄不算进去。

第二条是本文件最长的那个用例（`test_every_field_affects_hash`）在守。
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from typing import Any

import pytest

from app.benchmark.hashing import canonical_json, compute_content_hash, to_bare_hex
from app.benchmark.schema import TaskDefinition
from tests.unit.test_task_schema import load_fixture, make_task


def hash_of(**overrides: Any) -> str:
    # 换了 test_patch 就得让 test_patch_paths 跟着重算：那份清单是从补丁推导出来的，
    # 留着样例里的旧值会先撞上 C-74 的防篡改校验（这正是它该做的），
    # 于是就测不到"哈希变没变"了。
    if "test_patch" in overrides and "test_patch_paths" not in overrides:
        overrides["test_patch_paths"] = []
    task = TaskDefinition.model_validate(make_task(**overrides))
    assert task.content_hash is not None
    return task.content_hash


# ── 稳定性 ─────────────────────────────────────────────────


def test_field_order_does_not_matter() -> None:
    """AC：同内容不同字段序 → 同 hash。

    题目 JSON 是人写的、脚本生成的、从 SWE-bench 导入的，字段顺序千奇百怪。
    顺序影响哈希的话，同一道题从两个来源进来会被当成两道题。
    """
    base = make_task()
    shuffled = list(base.items())
    random.Random(20260903).shuffle(shuffled)

    assert hash_of() == TaskDefinition.model_validate(dict(shuffled)).content_hash


def test_list_order_does_not_matter() -> None:
    """F2P / P2P / tags 是集合，元素顺序不该影响哈希。

    实现上不是在哈希里排序，而是在模型解析时就排好——规范化留在数据里，
    看得见，而不是藏在哈希函数中。
    """
    ordered = ["tests/test_adapter.py::test_a", "tests/test_adapter.py::test_b"]
    assert hash_of(fail_to_pass=ordered, pass_to_pass=[]) == hash_of(
        fail_to_pass=list(reversed(ordered)), pass_to_pass=[]
    )


def test_roundtrip_keeps_hash() -> None:
    """存下来再读回去，哈希不变。不然每次导出导入都会"内容变了"。"""
    task = TaskDefinition.model_validate(load_fixture())
    again = TaskDefinition.model_validate(task.model_dump(mode="json"))
    assert again.content_hash == task.content_hash


def test_validation_block_is_excluded() -> None:
    """复验不改变题目内容，就不该改变哈希。

    `validation` 记的是"这题被验证过"的过程结果。数据集发布后每周复验一次，
    `validated_at` 和 `image_digest` 都会变。算进哈希的话，每复验一次
    全部数据集快照就集体失配——而题目其实一个字都没动。
    """
    with_validation = deepcopy(load_fixture())
    without = deepcopy(with_validation)
    without.pop("validation")

    changed = deepcopy(with_validation)
    changed["validation"]["validated_at"] = "2026-12-31T23:59:59Z"
    changed["validation"]["image_digest"] = "sha256:" + "cd" * 32

    hashes = {
        TaskDefinition.model_validate(payload).content_hash
        for payload in (with_validation, without, changed)
    }
    assert len(hashes) == 1


# ── 完整性：每个字段都要算进去 ──────────────────────────────

#: 改动这些字段应当让哈希变化。值是一个和样例不同的合法值。
FIELD_MUTATIONS: dict[str, Any] = {
    "task_id": "nonebot__nonebot2-9999",
    "dataset_id": "benchmark-cn-v1",
    "repo_url": "https://github.com/other/repo",
    "repo_name": "other/repo",
    "base_commit": "a" * 40,
    "environment_id": "nonebot2__py312__v1",
    "issue_title": "另一个标题",
    "issue_body": "另一段正文。" * 60,
    "issue_language": "en",
    "hints_text": "提示：看看 reconnect()",
    "install_command": "pip install -e .",
    "pre_test_command": "make build",
    "test_command": "pytest -q",
    "test_framework": "unittest",
    "test_report_path": "/tmp/other.xml",
    "test_patch": (
        "diff --git a/tests/test_other.py b/tests/test_other.py\n"
        "--- a/tests/test_other.py\n"
        "+++ b/tests/test_other.py\n"
        "@@ -1,1 +1,2 @@\n"
        " import pytest\n"
        "+# changed\n"
    ),
    "fail_to_pass": ["tests/test_adapter.py::test_something_else"],
    "pass_to_pass": [],
    "gold_patch": (
        "diff --git a/nonebot/other.py b/nonebot/other.py\n"
        "--- a/nonebot/other.py\n"
        "+++ b/nonebot/other.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    ),
    "agent_timeout_s": 900,
    "test_timeout_s": 600,
    "sandbox_cpu": 2.0,
    "sandbox_memory_mb": 2048,
    "sandbox_pids_limit": 256,
    "source_issue_url": "https://github.com/nonebot/nonebot2/issues/9999",
    "source_pr_url": "https://github.com/nonebot/nonebot2/pull/2314",
    "created_at_upstream": "2025-01-01T00:00:00Z",
    "language": "javascript",
    "framework": "fastapi",
    "difficulty": "hard",
    "tags": ["only-one-tag"],
}

#: 明确不参与"改了就该变哈希"检查的字段，每个都要有理由。
EXEMPT_FIELDS = {
    # 哈希的输出，不能同时是输入
    "content_hash",
    # 验证过程的结果，不是题目内容，见 test_validation_block_is_excluded
    "validation",
    # 由 test_patch 推导（C-74），单独改会被拒收；它跟着 test_patch 一起变
    "test_patch_paths",
    # Literal["1.0"]，只有一个合法值，改不了
    "schema_version",
}


def test_mutation_table_covers_every_field() -> None:
    """新加字段时，必须明确它算不算进哈希——这条用例逼人做这个决定。

    手工维护一份"判定相关字段"清单是会烂的：加了字段忘了加进清单，
    哈希就悄悄不覆盖它，而且没有任何报错。这里反过来，
    模型上的字段要么在变异表里，要么在豁免表里，漏一个就红。
    """
    model_fields = set(TaskDefinition.model_fields)
    covered = set(FIELD_MUTATIONS) | EXEMPT_FIELDS
    assert model_fields - covered == set(), "有新字段没在变异表或豁免表里登记"
    assert covered - model_fields == set(), "变异表里有模型上不存在的字段"


@pytest.mark.parametrize("field", sorted(FIELD_MUTATIONS))
def test_every_field_affects_hash(field: str) -> None:
    """改任何一个字段，哈希都必须变。

    哪个字段漏算了，那个字段就能被人悄悄改掉而数据集快照察觉不到——
    "可复现"当场变成一句口号。
    """
    assert hash_of(**{field: FIELD_MUTATIONS[field]}) != hash_of(), f"{field} 没被算进哈希"


# ── 格式 ───────────────────────────────────────────────────


def test_hash_format() -> None:
    """§7.1 的 JSON 里写的是 `sha256:...`。"""
    value = hash_of()
    assert value.startswith("sha256:")
    assert len(value) == len("sha256:") + 64


def test_to_bare_hex_fits_database_column() -> None:
    """`benchmark_tasks.content_hash` 是 CHAR(64)，装不下带前缀的 71 个字符。

    JSON 带前缀、数据库不带，这是既定事实（§7.1 vs 迁移 0001）。
    转换只在一个地方做，别让每个调用点各写一遍字符串切片。
    """
    bare = to_bare_hex(hash_of())
    assert len(bare) == 64
    assert int(bare, 16) >= 0  # 全是十六进制字符
    assert to_bare_hex(bare) == bare, "已经是裸十六进制时应当原样返回"


def test_canonical_json_keeps_chinese_readable() -> None:
    """规范 JSON 不转义中文。

    转成 \\uXXXX 之后哈希照样稳定，但人就没法直接看规范化结果对不对了，
    出问题时排查成本高很多。
    """
    text = canonical_json({"b": 1, "a": "断线重连"})
    assert text == '{"a":"断线重连","b":1}'


def test_canonical_json_drops_excluded_fields() -> None:
    payload = {"a": 1, "content_hash": "sha256:x", "validation": {"state": "VALID"}}
    assert json.loads(canonical_json(payload)) == {"a": 1}


def test_compute_is_pure() -> None:
    """同样的输入算两次结果一样——没有时间戳、随机数之类的东西混进来。"""
    payload = {"z": [3, 1, 2], "a": {"n": None}}
    assert compute_content_hash(payload) == compute_content_hash(payload)
