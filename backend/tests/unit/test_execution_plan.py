"""`ExecutionPlan` 与 `TaskDefinition.execution_plan()` 的单测（E4-T2）。

这一层管的是"题目 → 测试执行器"这道转换。它只有一处实现，但错了不会报错：
少映一个字段，执行器拿到的是默认值，测试照样跑得起来，只是跑错了预算或者
漏了一批受保护路径 —— 而漏受保护路径会让**解决率静悄悄地偏高**。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from app.benchmark.schema import TaskDefinition
from app.domain.execution_plan import ExecutionPlan

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "sample_task.json"


def load_task() -> TaskDefinition:
    return TaskDefinition.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_test_ids_are_f2p_then_p2p() -> None:
    """先 F2P 后 P2P，保持题目里写的顺序。"""
    plan = ExecutionPlan(
        base_commit="a" * 40,
        test_patch="",
        test_patch_paths=(),
        fail_to_pass=("t.py::b", "t.py::a"),
        pass_to_pass=("t.py::c",),
        test_command="pytest",
        test_report_path="report/junit.xml",
    )
    assert plan.test_ids == ("t.py::b", "t.py::a", "t.py::c")


def test_test_ids_deduplicate() -> None:
    """同一条用例同时出现在 F2P 和 P2P 里时只跑一遍。

    不去重的话 pytest 会跑两遍，junitxml 里就有两条同名 testcase。
    解析器按"先来后到"取第一条，结果是确定的，但报告里凭空多一条，
    看的人会以为哪里错了。
    """
    plan = ExecutionPlan(
        base_commit="a" * 40,
        test_patch="",
        test_patch_paths=(),
        fail_to_pass=("t.py::a",),
        pass_to_pass=("t.py::a", "t.py::b"),
        test_command="pytest",
        test_report_path="report/junit.xml",
    )
    assert plan.test_ids == ("t.py::a", "t.py::b")


def test_execution_plan_copies_every_field_it_needs() -> None:
    """题目里跟"跑测试"有关的字段都要映过去，一个都不能漏。"""
    task = load_task()
    plan = task.execution_plan()

    assert plan.base_commit == task.base_commit
    assert plan.test_patch == task.test_patch
    assert plan.test_patch_paths == tuple(task.test_patch_paths)
    assert plan.fail_to_pass == tuple(task.fail_to_pass)
    assert plan.pass_to_pass == tuple(task.pass_to_pass)
    assert plan.test_command == task.test_command
    assert plan.test_report_path == task.test_report_path
    assert plan.pre_test_command == task.pre_test_command
    assert plan.test_timeout_s == task.test_timeout_s
    assert plan.sandbox_cpu == task.sandbox_cpu
    assert plan.sandbox_memory_mb == task.sandbox_memory_mb
    assert plan.sandbox_pids_limit == task.sandbox_pids_limit
    assert plan.task_id == task.task_id


def test_execution_plan_does_not_carry_the_answer() -> None:
    """`gold_patch` 和 `issue_body` 绝不能跟着进执行计划。

    跑一轮测试用不着官方答案。带上它只是多一条泄漏路径，而这类泄漏一旦发生，
    整批实验数据都得作废 —— 事后没法证明某次运行到底看没看见答案。
    """
    task = load_task()
    plan = task.execution_plan()

    values = [getattr(plan, f.name) for f in dataclasses.fields(plan)]
    assert task.gold_patch not in values
    assert task.issue_body not in values
    # 字段名里也不该出现这两样
    names = {f.name for f in dataclasses.fields(plan)}
    assert "gold_patch" not in names
    assert "issue_body" not in names
    assert "hints_text" not in names


def test_extra_protected_paths_come_from_the_caller() -> None:
    """环境规格上的额外受保护路径由调用方传进来 —— 题目本身没有这个字段。"""
    task = load_task()

    assert task.execution_plan().extra_protected_paths == ()
    plan = task.execution_plan(extra_protected_paths=("docs/conf.py",))
    assert plan.extra_protected_paths == ("docs/conf.py",)


def test_plan_is_frozen() -> None:
    """执行计划是只读的。执行到一半被人改掉预算的话，落库的那份记录就不可信了。"""
    plan = load_task().execution_plan()
    try:
        plan.test_timeout_s = 1  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ExecutionPlan 应该是 frozen 的")
