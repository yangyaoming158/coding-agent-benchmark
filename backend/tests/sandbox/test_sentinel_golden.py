"""哨兵在 Golden 集上的实际落点（E3-T2 的验收标准）。

这一组**真的**物化工作区、真的打补丁、真的起 pytest 子进程。上一层
（`tests/unit/test_sentinel_runners.py`）只验补丁长什么样；这里验的是
补丁进了判定链之后到底发生了什么。

## 为什么必须真跑一遍

E3-T2 的验收标准写的是"Oracle 在 Golden 集上解决率 100%，Noop 0%"。
光靠"Oracle 交出来的补丁 == task.gold_patch"是推不出这个结论的——那只证明了
适配器没改补丁，证明不了这份补丁经过物化、apply、跑测试之后还能让 F2P 全过。
中间任何一步（行尾、编码、`git apply` 参数、用例 ID 归一化）出问题，
解决率都会掉，而这正是哨兵要拦住的事。

## 这里的"解决"是临时判据，不是判定引擎

真正的判定引擎是 E4-T3。这里用的是它的最小形态：

    补丁打不上           → 没解决（对应 INVALID_PATCH）
    F2P + P2P 全过       → 解决
    其余                 → 没解决

E4-T3 就位之后，这个文件里的 `judge_resolved()` 应该换成调真正的判定引擎，
断言不用动。

## 一处已知的临时做法

打的是**原始补丁**，还没经过 E3-T3 的受保护路径过滤。所以
`protected_path_edit` 那份补丁里的 `tests/test_mock_agent.py` 会真的落进
测试工作区。这里不影响结论（跑的是指定的用例 ID，不是整个套件），
E3-T3 就位后这一步要换成归一化后的补丁。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import agent_visible_patterns, enforcement_patterns, protected_hits
from app.runner.adapters import MockBehavior, MockRunner, NoopRunner, OracleRunner
from app.runner.protocol import (
    AgentConfig,
    AgentRunner,
    AgentTaskInput,
    Constraints,
    IssueInput,
    ModelInput,
    RepoInput,
    assert_no_leak,
)
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import materialize_workspace
from cli.golden import PYTEST_OK, apply_patch, build, load_tasks, patch_applies, run_pytest

TASKS = load_tasks()
TASK_IDS = [task.task_id for task in TASKS]
GOLD_PATCHES = {task.task_id: task.gold_patch for task in TASKS}

#: 六种行为都在同一道题上验。跑四道题只是把同一件事重复四遍，
#: 却要多起 18 个 pytest 子进程 —— Oracle 和 Noop 才是需要逐题验的那两个。
BEHAVIOR_TASK = TASKS[0]


# ── 把一道题转成下发给适配器的任务输入 ──────────────────────


def build_task_input(task: TaskDefinition, *, deadline_ms: int | None = None) -> AgentTaskInput:
    """`TaskDefinition` → `AgentTaskInput`。

    正式版本是 E5 编排层的事（它还要读环境规格里的网络策略、按 agent_config
    决定模型）。这里是够用的最小实现，但**防泄题一条都不打折**：

    - `protected_paths` 用 `agent_visible_patterns()`，不是 `enforcement_patterns()`
      —— 后者含该题的 `test_patch_paths`，下发出去等于告诉 AI 官方改了哪几个文件
      来验证（协议 C-76）；
    - `gold_patch` / `test_patch` / `fail_to_pass` / `test_command` 一个都不进去；
    - 组装完再过一遍 `assert_no_leak()` 兜底。
    """
    deadline = deadline_ms or int((time.time() + task.agent_timeout_s) * 1000)
    task_input = AgentTaskInput(
        task_id=task.task_id,
        issue=IssueInput(
            title=task.issue_title, body=task.issue_body, language=task.issue_language
        ),
        repo=RepoInput(name=task.repo_name, base_commit=task.base_commit),
        hints=task.hints_text,
        constraints=Constraints(
            deadline_unix_ms=deadline,
            protected_paths=list(agent_visible_patterns()),
        ),
        model=ModelInput(name="none"),
    )
    assert_no_leak(task_input.model_dump(mode="json"))
    return task_input


# ── 最小判定：补丁 → 解决 / 没解决 ──────────────────────────


@dataclass(frozen=True)
class SentinelOutcome:
    """一次哨兵运行的落点。字段都是给断言失败时看的。"""

    task_id: str
    patch_bytes: int
    #: 补丁能不能干净地打上。空补丁算能（没什么可打的）。
    applied: bool
    resolved: bool
    detail: str


def run_sentinel(
    task: TaskDefinition,
    runner: AgentRunner,
    *,
    mirror_root: Path,
    scratch: Path,
    deadline_ms: int | None = None,
) -> SentinelOutcome:
    """跑一遍完整的"物化 → 交补丁 → 换个干净工作区 → 打补丁 → 跑测试"。

    测试工作区是**新物化的一份**，不复用 Agent 那份（协议 C-15）。
    Agent 可能在自己那份里装过东西、留过临时文件，拿它跑测试，
    测出来的就不只是"补丁对不对"了。哨兵其实不碰工作区，但流程要照着真的走
    —— 这段代码将来会被 E4 换成真家伙，形状先对上。
    """
    mirror = MirrorManager(mirror_root).path_for(task.repo_name)
    agent_ws = materialize_workspace(
        mirror_path=mirror, base_commit=task.base_commit, dest=scratch / "agent"
    )
    result = runner.run(build_task_input(task, deadline_ms=deadline_ms), agent_ws, AgentConfig())

    test_ws = materialize_workspace(
        mirror_path=mirror, base_commit=task.base_commit, dest=scratch / "test"
    )
    patch_file = scratch / "agent.patch"
    if result.has_patch:
        if not patch_applies(test_ws, patch_file, result.patch):
            return SentinelOutcome(
                task.task_id, len(result.patch), False, False, "补丁打不上（INVALID_PATCH）"
            )
        apply_patch(test_ws, patch_file, result.patch)

    apply_patch(test_ws, scratch / "test.patch", task.test_patch)
    code = run_pytest(test_ws, [*task.fail_to_pass, *task.pass_to_pass])
    resolved = code == PYTEST_OK
    return SentinelOutcome(
        task.task_id,
        len(result.patch),
        True,
        resolved,
        f"F2P + P2P 一起跑，pytest 退出码 {code}",
    )


# ── fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """现建一套 golden 镜像，不用开发机上 `var/mirrors` 里那份。

    那份可能是几天前 build 的，源码改了却忘了重新生成时，测试会拿旧镜像跑出绿灯。
    """
    root = tmp_path_factory.mktemp("sentinel-mirrors")
    build(mirror_root=root)
    return root


# ── Oracle：解决率必须 100% ─────────────────────────────────


@pytest.mark.parametrize("task", TASKS, ids=TASK_IDS)
def test_oracle_resolves_every_golden_task(
    task: TaskDefinition, mirror_root: Path, tmp_path: Path
) -> None:
    """Oracle 哨兵：每道题都必须解决（协议 C-50）。

    哪道题过不了，说明的不是"Oracle 不行"，而是这三件事之一：题坏了、
    补丁应用这一步有问题、或者用例 ID 对不上。三种都必须在发数据集之前查清。
    """
    outcome = run_sentinel(
        task, OracleRunner(GOLD_PATCHES), mirror_root=mirror_root, scratch=tmp_path
    )
    assert outcome.applied, f"{task.task_id}：官方补丁都打不上，{outcome.detail}"
    assert outcome.resolved, f"{task.task_id}：Oracle 没解决，{outcome.detail}"
    assert outcome.patch_bytes > 0


# ── Noop：解决率必须 0% ─────────────────────────────────────


@pytest.mark.parametrize("task", TASKS, ids=TASK_IDS)
def test_noop_resolves_no_golden_task(
    task: TaskDefinition, mirror_root: Path, tmp_path: Path
) -> None:
    """Noop 哨兵：一道都不能解决（协议 C-50）。

    解决了就说明那道题在修复前 F2P 就已经通过，它没有区分度 ——
    整个排行榜的下限会跟着虚高。
    """
    outcome = run_sentinel(task, NoopRunner(), mirror_root=mirror_root, scratch=tmp_path)
    assert outcome.patch_bytes == 0
    assert not outcome.resolved, f"{task.task_id}：空补丁居然解决了，{outcome.detail}"


# ── Mock：六种行为各自落在哪 ────────────────────────────────


def test_mock_correct_patch_resolves(mirror_root: Path, tmp_path: Path) -> None:
    """正确补丁 → 解决。这条同时证明 Mock 的 correct_patch 和 Oracle 是同一条路。"""
    runner = MockRunner(MockBehavior.CORRECT_PATCH, patches=GOLD_PATCHES)
    outcome = run_sentinel(BEHAVIOR_TASK, runner, mirror_root=mirror_root, scratch=tmp_path)
    assert outcome.resolved, outcome.detail


def test_mock_wrong_patch_applies_but_does_not_resolve(mirror_root: Path, tmp_path: Path) -> None:
    """错误补丁 → 打得上，但没解决（UNRESOLVED）。

    "打得上"这一半很重要：打不上的话它就变成 INVALID_PATCH 了，
    那是另一条判定分支，这种行为就白造了。
    """
    runner = MockRunner(MockBehavior.WRONG_PATCH)
    outcome = run_sentinel(BEHAVIOR_TASK, runner, mirror_root=mirror_root, scratch=tmp_path)
    assert outcome.applied, "错误补丁应该能干净地打上"
    assert outcome.patch_bytes > 0
    assert not outcome.resolved


def test_mock_empty_patch_does_not_resolve(mirror_root: Path, tmp_path: Path) -> None:
    runner = MockRunner(MockBehavior.EMPTY_PATCH)
    outcome = run_sentinel(BEHAVIOR_TASK, runner, mirror_root=mirror_root, scratch=tmp_path)
    assert outcome.patch_bytes == 0
    assert not outcome.resolved


def test_mock_timeout_does_not_resolve(mirror_root: Path, tmp_path: Path) -> None:
    """超时 → 空手而归，没解决。deadline 特意设在 50 毫秒之后，让它真的走过截止时刻。"""
    runner = MockRunner(MockBehavior.TIMEOUT, max_sleep_s=0.2)
    outcome = run_sentinel(
        BEHAVIOR_TASK,
        runner,
        mirror_root=mirror_root,
        scratch=tmp_path,
        deadline_ms=int(time.time() * 1000) + 50,
    )
    assert outcome.patch_bytes == 0
    assert not outcome.resolved


def test_mock_malformed_patch_fails_to_apply(mirror_root: Path, tmp_path: Path) -> None:
    """非法补丁 → 非空，但 `git apply` 打不上（INVALID_PATCH）。

    这是 `malformed_patch` 这种行为的**全部意义**：只有真的打不上，
    判定链才会走到 PATCH_APPLY_FAILED / INVALID_PATCH 那条分支上。
    """
    runner = MockRunner(MockBehavior.MALFORMED_PATCH)
    outcome = run_sentinel(BEHAVIOR_TASK, runner, mirror_root=mirror_root, scratch=tmp_path)
    assert outcome.patch_bytes > 0, "非法补丁必须非空，空补丁走的是另一条分支"
    assert not outcome.applied, outcome.detail
    assert not outcome.resolved


def test_mock_protected_path_edit_keeps_the_evidence(mirror_root: Path, tmp_path: Path) -> None:
    """改受保护文件 → 那条改动**留在原始补丁里**，并且平台认得出来。

    适配器自己过滤掉的话，`protected_path_edit_attempted` 就没有证据了
    （协议 C-08b）。过滤是平台在 E3-T3 做的事。
    """
    runner = MockRunner(MockBehavior.PROTECTED_PATH_EDIT)
    mirror = MirrorManager(mirror_root).path_for(BEHAVIOR_TASK.repo_name)
    workspace = materialize_workspace(
        mirror_path=mirror, base_commit=BEHAVIOR_TASK.base_commit, dest=tmp_path / "agent"
    )
    result = runner.run(build_task_input(BEHAVIOR_TASK), workspace, AgentConfig())

    paths = tuple(derive_patch_paths(result.patch))
    hits = protected_hits(paths, enforcement_patterns(tuple(BEHAVIOR_TASK.test_patch_paths)))
    assert hits, f"补丁里没留下受保护路径的痕迹：{paths}"
    assert patch_applies(workspace, tmp_path / "raw.patch", result.patch), "这份补丁本身要能打上"
