"""三个哨兵适配器过契约套件（E3-T2）。

一共八个类，六条契约各跑一遍：Oracle、Noop，加上 Mock 的六种行为各一个。

**六种行为各占一个类，不是为了凑数。** 契约第 2 条要求 `has_patch` 和
`produces_patch` 这句声明严格对上，所以"交补丁"和"交空手"的行为必须分开声明；
合成一个类的话，只能挑一种行为跑，另外五种就从来没被契约验过。

接一个新适配器长什么样，看 `_MockContract`：给一个 `runner` fixture 就完事。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.runner.adapters import MockBehavior, MockRunner, NoopRunner, OracleRunner
from app.runner.adapters.base import GOLD_PATCH_MISSING
from app.runner.adapters.mock import new_file_diff
from app.runner.protocol import AgentConfig, AgentRunner
from tests.contract.runner_contract import AgentRunnerContract, make_task_input

#: `make_task_input()` 默认造的那道题的 id。Oracle 和 Mock 的 correct_patch
#: 都要按它查补丁，查不到就交空手，契约第 2 条会直接红——这正是我们想要的压力。
CONTRACT_TASK_ID = "bench-contract__demo-1"


def gold_like_diff(path: str = "src/auth.py") -> str:
    """一段长得像官方补丁的 diff：改一个已有源文件的一行。

    内容不重要，契约只要求它解析得出路径。形状按真实 gold_patch 来写，
    是为了不给后面接真实适配器的人做坏示范。
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " def login(user, password):\n"
        "-    return True\n"
        "+    return bool(password) and password == user.password\n"
    )


GOLD_PATCHES = {CONTRACT_TASK_ID: gold_like_diff()}


# ── Oracle ──────────────────────────────────────────────────


class TestOracleRunner(AgentRunnerContract):
    """Oracle：喂什么官方补丁就交什么。"""

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return OracleRunner(GOLD_PATCHES)

    def runner_that_edits(self, relative_path: str) -> AgentRunner:
        """第 4 条：喂一份改了受保护路径的"官方补丁"，Oracle 必须原样交出来。

        Oracle 自己不做任何过滤，这个场景直接证明了这一点——它连"这个补丁碰了
        tests/"都不去看。过滤是平台的事（协议 C-41、C-08b）。
        """
        return OracleRunner({CONTRACT_TASK_ID: new_file_diff(relative_path, ["assert True"])})


def test_oracle_without_patches_reports_it(tmp_path: Path) -> None:
    """没喂补丁时明着报错，不是悄悄交空手。

    静默交空手的话，这道题会被判成 UNRESOLVED，解决率从 100% 掉下来，
    而排查的人会去翻判定引擎——真正的原因在那里根本看不出来。
    """
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 60_000)
    result = OracleRunner().run(task, tmp_path, AgentConfig())

    assert not result.has_patch
    assert result.exit_code == 1
    assert result.error is not None and result.error.code == GOLD_PATCH_MISSING


# ── Noop ────────────────────────────────────────────────────


class TestNoopRunner(AgentRunnerContract):
    """Noop：交空补丁是它的定义，不是缺陷。"""

    produces_patch = False

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return NoopRunner()


# ── Mock 的六种行为 ─────────────────────────────────────────


class _MockContract(AgentRunnerContract):
    """所有 Mock 行为共用的接法。子类只需要改两个类属性。

    类名不以 `Test` 开头，pytest 不会收集它——收集了的话，这六条会在
    `behavior` 还是默认值的情况下多跑一遍。
    """

    behavior: MockBehavior = MockBehavior.CORRECT_PATCH

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return MockRunner(self.behavior, patches=GOLD_PATCHES)

    def runner_that_edits(self, relative_path: str) -> AgentRunner:
        """第 4 条的场景就是 Mock 的第六种行为，直接切过去。"""
        return MockRunner(MockBehavior.PROTECTED_PATH_EDIT, protected_target=relative_path)


class TestMockCorrectPatch(_MockContract):
    behavior = MockBehavior.CORRECT_PATCH


class TestMockWrongPatch(_MockContract):
    behavior = MockBehavior.WRONG_PATCH


class TestMockMalformedPatch(_MockContract):
    """非法补丁仍然是**非空**补丁（协议：INVALID_PATCH = 非空但打不上）。

    所以这里的 `produces_patch` 是 True。契约第 2 条还会顺带验证它解析得出路径——
    拿一段自然语言冒充 diff 是另一回事，不是这种行为要模拟的东西。
    """

    behavior = MockBehavior.MALFORMED_PATCH


class TestMockProtectedPathEdit(_MockContract):
    behavior = MockBehavior.PROTECTED_PATH_EDIT


class TestMockEmptyPatch(_MockContract):
    behavior = MockBehavior.EMPTY_PATCH
    produces_patch = False


class TestMockTimeout(_MockContract):
    behavior = MockBehavior.TIMEOUT
    produces_patch = False
