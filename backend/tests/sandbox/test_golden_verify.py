"""Golden 题的六步验证（E1-T2 的验收标准）。

这一组真的会物化工作区、打补丁、起 pytest 子进程，四道题跑完大约 5 秒。
慢是应该的 —— 它验的是"这批题自己站得住"，而 Week 1 的判定引擎、沙箱、
Runner 适配器全都拿这批题当已知答案。

末尾两条是**反向用例**：故意把题弄坏，确认验证器真的会红。没有这两条的话，
一个永远返回"通过"的验证器也能让上面的用例全绿。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from cli.golden import StepResult, build, load_tasks, verify_task

TASKS = load_tasks()
TASK_IDS = [task.task_id for task in TASKS]


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """在临时目录里建一套 golden 镜像。

    不用开发机上 `var/mirrors` 里的那份：那份可能是几天前 build 的，
    源码改了却忘了重新生成时，测试会拿旧镜像跑出绿灯。每次现建，贵不了一秒。
    """
    root = tmp_path_factory.mktemp("golden-mirrors")
    build(mirror_root=root)
    return root


@pytest.fixture(scope="module")
def all_steps(mirror_root: Path) -> dict[str, list[StepResult]]:
    """四道题各跑一遍六步验证，结果缓存给整个模块用。

    跑一遍要几秒（真的在起 pytest 子进程），下面几条用例只是从不同角度读同一批
    结论，没必要各自重跑一次。
    """
    return {task.task_id: verify_task(task, mirror_root=mirror_root) for task in TASKS}


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_six_steps_all_pass(task_id: str, all_steps: dict[str, list[StepResult]]) -> None:
    """六步全过 —— E1-T2 的第一条验收标准。"""
    steps = all_steps[task_id]
    assert len(steps) == 6
    failed = [f"{s.number}. {s.name} —— {s.detail}" for s in steps if not s.passed]
    assert not failed, f"{task_id} 没过：" + "；".join(failed)


def test_oracle_solves_every_task(all_steps: dict[str, list[StepResult]]) -> None:
    """Oracle 解决率 100%：官方补丁打上去之后，每道题的 F2P 和 P2P 都通过。

    对应第 5、6 步。不是 100% 就说明有坏题或者补丁拆错了（协议 C-50）。
    """
    for task_id, steps in all_steps.items():
        oracle = [s for s in steps if s.number in (5, 6)]
        assert all(s.passed for s in oracle), f"{task_id} 上 Oracle 没解决"


def test_noop_solves_nothing(all_steps: dict[str, list[StepResult]]) -> None:
    """Noop 解决率 0%：空补丁下每道题的 F2P 都还是失败的。

    对应第 3 步。哪道题在修复前 F2P 就已经通过，它就没有区分度，
    Noop 哨兵会给出非零解决率（协议 C-50）。
    """
    for task_id, steps in all_steps.items():
        step3 = next(s for s in steps if s.number == 3)
        assert step3.passed, f"{task_id} 上 Noop 居然解决了：{step3.detail}"


# ── 反向用例：把题弄坏，验证器必须红 ────────────────────────


def test_verifier_catches_f2p_that_already_passes(mirror_root: Path) -> None:
    """把一条本来就通过的用例塞进 F2P，第 3 步必须报出来。

    这是最该被拦住的坏题形态：F2P 在修复前就通过，等于这道题不用改代码也算解决，
    Noop 哨兵会因此得到非零解决率，整个排行榜的下限就不可信了。
    """
    good = TASKS[0]
    broken = good.model_copy(update={"fail_to_pass": [good.pass_to_pass[0]], "pass_to_pass": []})
    step3 = next(s for s in verify_task(broken, mirror_root=mirror_root) if s.number == 3)
    assert not step3.passed
    assert "修复前就已经通过" in step3.detail


def test_verifier_catches_missing_test_case(mirror_root: Path) -> None:
    """F2P 里写了一个不存在的用例 ID，第 3 步必须说"用例根本不存在"。

    区分"用例挂了"和"用例不存在"很重要：后者是 ID 写错或者 test_patch 没加上，
    混为一谈的话，一道根本没跑过测试的坏题会被当成好题放进数据集。
    """
    good: TaskDefinition = TASKS[0]
    broken = good.model_copy(
        update={"fail_to_pass": ["tests/test_nothing.py::test_does_not_exist"]}
    )
    step3 = next(s for s in verify_task(broken, mirror_root=mirror_root) if s.number == 3)
    assert not step3.passed
    assert "根本不存在" in step3.detail
