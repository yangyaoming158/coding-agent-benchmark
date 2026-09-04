"""Golden 数据集本身合不合格（E1-T2，快的那一半）。

这一组只读文件、不跑测试，毫秒级。真正把四道题跑一遍的六步验证在
`tests/sandbox/test_golden_verify.py` 里。

**最要紧的是 `test_generated_json_matches_sources`**：任务 JSON 是从
`datasets/golden/sources/` 生成的，两边一旦漂移，别人读源码目录以为改了题，
实际跑的还是旧 JSON。这条把漂移变成一次红灯。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from cli.golden import (
    ENVIRONMENTS_DIR,
    GOLDEN_ROOT,
    SOURCES_DIR,
    build,
    check_build,
    iter_sources,
    load_environments,
    load_tasks,
)

#: E1-T2 要求 3–5 道。定成区间而不是钉死数量：加一道题不该让测试红，
#: 但"只剩一道"或者"塞了二十道"都说明这批题的定位跑偏了。
MIN_TASKS = 3
MAX_TASKS = 5


@pytest.fixture(scope="module")
def tasks() -> list[TaskDefinition]:
    return load_tasks()


def test_dataset_size(tasks: list[TaskDefinition]) -> None:
    assert MIN_TASKS <= len(tasks) <= MAX_TASKS


def test_every_json_parses_and_hash_checks_out(tasks: list[TaskDefinition]) -> None:
    """能被 `TaskDefinition` 解析出来，就等于 §7 那一整套规则都过了。

    `content_hash` 也在这一步核对：JSON 里写着的哈希和重算的对不上就直接抛错，
    所以这条同时证明了"文件没被手工改过"。
    """
    assert tasks
    for task in tasks:
        assert task.content_hash is not None
        assert task.content_hash.startswith("sha256:")


def test_generated_json_matches_sources(tmp_path: Path) -> None:
    """仓库里的任务 JSON 必须是当前 `sources/` 生成出来的。

    镜像建到 tmp_path 下，不动开发机上 var/mirrors 里的那份 —— 测试不该有副作用。
    """
    problems = check_build(build(mirror_root=tmp_path / "mirrors"))
    assert not problems, "跑 `make golden` 重新生成：" + "；".join(problems)


def test_task_ids_match_source_directories(tasks: list[TaskDefinition]) -> None:
    from_json = sorted(task.task_id for task in tasks)
    from_sources = sorted(source.task_id for source in iter_sources())
    assert from_json == from_sources


def test_no_review_flags(tasks: list[TaskDefinition]) -> None:
    """手写的题不该有任何需要人工复核的地方。

    `review_flags()` 会挑三种毛病：issue 太短、F2P 太多、没有 P2P。
    真实挖来的题带上这些标记很正常，golden 是我们自己写的，没有借口。
    """
    for task in tasks:
        assert task.review_flags() == [], f"{task.task_id} 有待复核项"


def test_issues_are_chinese(tasks: list[TaskDefinition]) -> None:
    """中文 issue 是这个项目的公开指标之一，golden 全部用中文写。"""
    for task in tasks:
        assert task.issue_language == "zh"
        assert any("一" <= ch <= "鿿" for ch in task.issue_title)
        assert any("一" <= ch <= "鿿" for ch in task.issue_body)


def test_issue_does_not_contain_the_answer(tasks: list[TaskDefinition]) -> None:
    """issue 里不能出现补丁内容。

    模型构造时已经拦了 PR 链接和 diff 代码块，这里再查一条它查不到的：
    issue 正文里不该出现 gold_patch 新增的那些代码行。手写 issue 时描述"期望行为"
    很容易顺手把实现贴进去，那等于把答案发下去了。
    """
    for task in tasks:
        added = [
            line[1:].strip()
            for line in task.gold_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        # 只看有实质内容的行；`return` 这种到处都是的短行不算泄题
        meaningful = [line for line in added if len(line) > 30]
        leaked = [line for line in meaningful if line in task.issue_body]
        assert not leaked, f"{task.task_id} 的 issue 里贴了 gold_patch 的代码行：{leaked[:2]}"


def test_fail_and_pass_sets_are_disjoint_and_nonempty(tasks: list[TaskDefinition]) -> None:
    for task in tasks:
        assert task.fail_to_pass, f"{task.task_id} 没有 F2P"
        assert task.pass_to_pass, f"{task.task_id} 没有 P2P（回归护栏）"
        assert not set(task.fail_to_pass) & set(task.pass_to_pass)


def test_environments_cover_every_task(tasks: list[TaskDefinition]) -> None:
    """每道题引用的 environment_id 都要在清单里找得到。

    题目只记 environment_id 不记镜像（§7.1），对不上的话建镜像那一步会拿不到规格。
    """
    known = {env["environment_id"] for env in load_environments()}
    for task in tasks:
        assert task.environment_id in known, f"{task.task_id} 引用了不存在的环境规格"


def test_budgets_keep_the_suite_fast(tasks: list[TaskDefinition]) -> None:
    """Golden 的定位是"几秒钟跑完的验证基石"，测试预算按 60 秒卡死。

    Week 1 的每一轮自测都要跑这批题，预算放宽了，坏掉的题会以"超时"的形式
    慢慢拖长每一次自测，而不是当场报错。
    """
    for task in tasks:
        assert task.test_timeout_s <= 60


def test_sources_have_no_stray_files() -> None:
    """源码目录只允许有约定的这四样，多出来的说明谁放错了地方。"""
    allowed = {"task.toml", "issue.md", "base", "fix"}
    for source in iter_sources():
        actual = {entry.name for entry in source.path.iterdir()}
        assert actual == allowed, f"{source.path} 里有意料之外的东西：{actual - allowed}"


def test_golden_root_holds_only_generated_json() -> None:
    """`datasets/golden/*.json` 必须全是任务文件。

    这条盯的是 `cli.task import datasets/golden/*.json` 这个标准用法：
    往这个目录里塞一个不是任务的 JSON（环境规格就差点放在这儿），导入命令会拒收它，
    看起来像题库里混进了坏题。环境规格因此单独放一个子目录。
    """
    known = {SOURCES_DIR.name, ENVIRONMENTS_DIR.name, "README.md"}
    unexpected = {p.name for p in GOLDEN_ROOT.iterdir()} - known
    assert unexpected == {f"{task.task_id}.json" for task in load_tasks()}
