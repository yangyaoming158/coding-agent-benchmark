"""`StoredPatchRunner` 过契约套件（E5-T1，协议 C-54）。

它不是哨兵，但它会被当成适配器塞进 `execute_task_run()`，所以同样要过那六条——
"跑一次评测"这条链上，任何一个 `AgentRunner` 都必须表现一致。

这里另外验三件 C-54 特有的事：交出来的补丁是原样的、成本报 0、名字不冒充原 Agent。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.domain.enums import CostSource
from app.runner.adapters.mock import new_file_diff
from app.runner.adapters.stored import (
    STORED_PATCH_MISSING,
    STORED_PATCH_VERSION,
    StoredPatchRunner,
)
from app.runner.protocol import AgentConfig, AgentRunner, AgentRunResult
from tests.contract.runner_contract import AgentRunnerContract, make_task_input

#: `make_task_input()` 默认造的那道题的 id。
CONTRACT_TASK_ID = "bench-contract__demo-1"

#: 假装是上一次 attempt 留下来的标准化补丁。
PREVIOUS_PATCH = (
    "diff --git a/src/auth.py b/src/auth.py\n"
    "--- a/src/auth.py\n"
    "+++ b/src/auth.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def login(user, password):\n"
    "-    return True\n"
    "+    return bool(password) and password == user.password\n"
)


class TestStoredPatchRunner(AgentRunnerContract):
    """六条契约。"""

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return StoredPatchRunner({CONTRACT_TASK_ID: PREVIOUS_PATCH})

    def runner_that_edits(self, relative_path: str) -> AgentRunner:
        """第 4 条：上一次的补丁里要是有受保护路径的改动，也原样交出来。

        重放**不做任何过滤**，和 Oracle 同一个理由：过滤是平台的事（C-41）。
        而且这里更绝对——补丁是我们自己上一次存下来的，那时候已经过过一遍过滤了，
        这里再过一次只会掩盖"上一次过滤是不是漏了"。
        """
        return StoredPatchRunner({CONTRACT_TASK_ID: new_file_diff(relative_path, ["assert True"])})


# ── C-54 特有的三条 ─────────────────────────────────────────


def _run(runner: StoredPatchRunner, tmp_path: Path) -> AgentRunResult:
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 60_000)
    return runner.run(task, tmp_path, AgentConfig())


def test_patch_comes_back_byte_for_byte(tmp_path: Path) -> None:
    """交出来的必须和存进去的一模一样。

    差一个字节都不行：重试的全部意义是"用**同一份**补丁再跑一次测试"。
    补丁变了，这次就不是重跑，而是一次新实验，两次结果之间没有可比性。
    """
    result = _run(StoredPatchRunner({CONTRACT_TASK_ID: PREVIOUS_PATCH}), tmp_path)
    assert result.patch == PREVIOUS_PATCH


def test_replay_costs_nothing(tmp_path: Path) -> None:
    """重放报 0 成本，而且是"报得出来的 0"（`reported`，不是 `unavailable`）。

    协议 C-56 要求成本**累计全部 attempt**。这次确实一分钱没花，如实报 0；
    把上一次的成本再报一遍，同一笔钱就被计了两次，成本-解决率散点图会整体右移。
    """
    result = _run(StoredPatchRunner({CONTRACT_TASK_ID: PREVIOUS_PATCH}), tmp_path)
    assert result.cost_usd == 0.0
    assert result.cost_source is CostSource.REPORTED
    assert result.token_usage is not None and result.token_usage.total == 0


def test_it_does_not_impersonate_the_original_agent(tmp_path: Path) -> None:
    """名字是 `stored-patch`，不冒充原来那个 Agent。

    冒充的话，报表上这条 attempt 看起来就像"又跑了一次真实模型"，
    看的人会以为花了钱、也会以为这是一次独立采样。
    """
    result = _run(StoredPatchRunner({CONTRACT_TASK_ID: PREVIOUS_PATCH}), tmp_path)
    assert result.agent_name == "stored-patch"
    assert result.agent_version == STORED_PATCH_VERSION
    assert result.model is None


def test_missing_patch_is_reported_not_swallowed(tmp_path: Path) -> None:
    """查不到补丁就明着报错，不是悄悄交空手。

    交空手的话这道题会被判成 `EMPTY_PATCH`——一个**记在被测 AI 头上**的结论，
    而真正的原因是我们自己把制品弄丢了。
    """
    result = _run(StoredPatchRunner(), tmp_path)
    assert not result.has_patch
    assert result.exit_code == 1
    assert result.error is not None
    assert result.error.code == STORED_PATCH_MISSING
