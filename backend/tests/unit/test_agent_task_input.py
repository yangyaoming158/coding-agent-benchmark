"""`TaskDefinition.agent_task_input()` 的单测（E4-T4）。

这一层管的是**防泄题**。`AgentTaskInput` 里的每一个字段都会被被测 AI 看到，
漏一样出去，整批实验数据就得作废 —— 事后没法证明某次运行到底看没看见答案。

所以这里的断言方向是反的：不是"该有的都有"，而是**"不该有的一样都没有"**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.protected_paths import agent_visible_patterns, enforcement_patterns
from app.runner.protocol import FORBIDDEN_INPUT_KEYS

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "sample_task.json"

DEADLINE = 1_800_000_000_000


def load_task() -> TaskDefinition:
    return TaskDefinition.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def payload_text(task: TaskDefinition, **kwargs: object) -> str:
    """把下发内容整个序列化成一个字符串，用来做"某段文本出现了没有"的检查。"""
    return task.agent_task_input(deadline_unix_ms=DEADLINE, **kwargs).to_stdin_line()  # type: ignore[arg-type]


def test_answer_never_reaches_the_agent() -> None:
    """官方补丁一个字节都不能出现在下发内容里（协议 C-44）。"""
    task = load_task()
    assert task.gold_patch, "样例题没有 gold_patch，这条用例就白测了"
    assert task.gold_patch not in payload_text(task)


def test_verification_tests_never_reach_the_agent() -> None:
    """测试补丁、F2P/P2P 名单、测试命令都不能下发（C-76）。

    给了 F2P 名单，AI 就知道该让哪几条用例通过；给了 test_command，
    "自己摸索怎么跑测试"这项能力就测不到了（§9.2）。
    """
    task = load_task()
    text = payload_text(task)
    assert task.test_patch not in text
    assert task.test_command not in text
    for test_id in [*task.fail_to_pass, *task.pass_to_pass]:
        assert test_id not in text, f"F2P/P2P 用例 ID 泄漏了：{test_id}"


def test_repo_url_is_not_sent() -> None:
    """只给仓库名和 base commit，**不给 URL**。

    给了 URL，AI 一句 `git clone` 就能拉到官方修复 —— 工作区那边把 git 历史
    剥到只剩一个提交（E2-T1）的功夫就全白费了。
    """
    task = load_task()
    assert task.repo_url not in payload_text(task)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_INPUT_KEYS))
def test_no_forbidden_key_appears(key: str) -> None:
    """协议 C-76 那张禁止下发的键名清单，一个都不许出现。"""
    payload = json.loads(payload_text(load_task()))

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                assert name != key, f"下发内容里出现了禁止的键：{key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)


def test_protected_paths_are_the_agent_visible_list() -> None:
    """下发的受保护清单必须是"通用规则"那份，不是执行用的完整清单。

    完整清单含该题的 `test_patch_paths` —— 下发出去等于告诉 AI
    "官方改了这几个文件来验证你"，那是很强的提示（C-75、C-76）。
    """
    task = load_task()
    sent = task.agent_task_input(deadline_unix_ms=DEADLINE).constraints.protected_paths

    assert sent == list(agent_visible_patterns())
    full = enforcement_patterns(tuple(task.test_patch_paths))
    assert len(sent) < len(full) or not task.test_patch_paths
    for path in task.test_patch_paths:
        assert path not in sent


def test_extra_is_scanned_for_leaks() -> None:
    """`extra` 是塞私有配置的口子，也就成了泄题最容易发生的地方 —— 它也要被扫。"""
    task = load_task()
    with pytest.raises(ValueError, match="C-76"):
        task.agent_task_input(deadline_unix_ms=DEADLINE, extra={"fail_to_pass": ["x"]})


def test_issue_and_hints_do_reach_the_agent() -> None:
    """反过来，issue 是 AI **唯一**的需求来源，必须完整送到。"""
    task = load_task()
    sent = task.agent_task_input(deadline_unix_ms=DEADLINE)
    assert sent.issue.title == task.issue_title
    assert sent.issue.body == task.issue_body
    assert sent.hints == task.hints_text
    assert sent.repo.base_commit == task.base_commit


def test_deadline_and_model_are_passed_through() -> None:
    task = load_task()
    sent = task.agent_task_input(
        deadline_unix_ms=DEADLINE, model="claude-x", temperature=0.0, max_tokens_budget=1000
    )
    assert sent.constraints.deadline_unix_ms == DEADLINE
    assert sent.model.name == "claude-x"
    assert sent.constraints.max_tokens_budget == 1000
