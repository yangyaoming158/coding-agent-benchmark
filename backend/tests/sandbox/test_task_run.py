"""端到端评测单元的验收（E4-T4，**M1 里程碑**）。

真起容器、真跑 pytest、真判定。这一层证明的是**整条链路是通的**：

    物化工作区 → 跑被测 AI → 抓补丁+归一化 → 换一份干净工作区
      → 打补丁 → 强制还原受保护路径 → 打测试补丁 → 容器里跑测试
      → 解析报告 → 判定 → 落制品

两条哨兵是判卷标准（`AGENTS.md` §9）：

- **Oracle 哨兵**：用官方补丁跑四道 Golden 题，解决率必须 **100%**。
  不是 100% 就说明有坏题，或者判定链哪一环有 bug。
- **Noop 哨兵**：用空补丁跑，解决率必须 **0%**。
  不是 0% 说明有题目在修复前测试就已经通过了。

这两条一上一下，把整条链路的可信度框住了。它们比任何单元测试都值钱 ——
单测能证明每个零件对，只有哨兵能证明**装起来之后还对**。

落库那一半在 `tests/integration/test_task_run_persistence.py`（要数据库，不要 Docker）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.enums import AgentOutcome, InfraOutcome, LifecycleStatus, PatchKind
from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE
from app.evaluation.task_run import TaskRunInputs, TaskRunOutcome, deadline_ms, execute_task_run
from app.runner.adapters.mock import MockBehavior, MockRunner
from app.runner.adapters.noop import NoopRunner
from app.runner.adapters.oracle import OracleRunner
from app.runner.protocol import AgentRunner
from app.sandbox.container import DockerUnavailableError, get_docker_client
from app.storage.local import LocalArtifactStore

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "datasets" / "golden"
MIRROR_ROOT = REPO_ROOT / "var" / "mirrors"

#: Mock 的行为只在一道题上验。跑四道题只是把同一件事重复四遍，
#: 而 Oracle 和 Noop 才是需要逐题验的那两个。
BEHAVIOR_TASK_ID = "bench-golden__textkit-1"


def load_tasks() -> list[TaskDefinition]:
    return [
        TaskDefinition.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(GOLDEN_DIR.glob("bench-golden__*.json"))
    ]


TASKS = load_tasks()
GOLD_PATCHES = {task.task_id: task.gold_patch for task in TASKS}


@pytest.fixture(scope="module")
def image() -> str:
    try:
        client = get_docker_client()
    except DockerUnavailableError as exc:
        pytest.skip(f"Docker 不可用：{exc}")
    try:
        client.images.get(DEFAULT_GOLDEN_IMAGE)
    except Exception:
        pytest.skip(f"没有 {DEFAULT_GOLDEN_IMAGE}，先跑 `make images`")
    if not MIRROR_ROOT.exists():
        pytest.skip(f"没有镜像仓库 {MIRROR_ROOT}，先跑 `make golden`")
    return DEFAULT_GOLDEN_IMAGE


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def mirror_of(task: TaskDefinition) -> Path:
    return MIRROR_ROOT / f"{task.repo_name.replace('/', '__')}.git"


def run_once(
    task: TaskDefinition,
    runner: AgentRunner,
    *,
    image: str,
    tmp_path: Path,
    store: LocalArtifactStore | None = None,
    name: str = "run",
) -> TaskRunOutcome:
    """跑一次完整的评测单元。"""
    return execute_task_run(
        runner,
        TaskRunInputs(
            plan=task.execution_plan(),
            agent_input=task.agent_task_input(deadline_unix_ms=deadline_ms(120)),
            mirror_path=mirror_of(task),
            scratch_dir=tmp_path / name,
            image=image,
            run_key=f"test/{task.task_id}/{name}",
        ),
        store=store,
    )


def find(task_id: str) -> TaskDefinition:
    return next(task for task in TASKS if task.task_id == task_id)


# ── 两条哨兵 ────────────────────────────────────────────────


@pytest.mark.parametrize("task", TASKS, ids=[t.task_id for t in TASKS])
def test_oracle_sentinel_resolves(task: TaskDefinition, image: str, tmp_path: Path) -> None:
    """Oracle 哨兵：官方补丁必须判成修好。

    不是 100% 就说明要么有坏题，要么判定链上哪一环有 bug ——
    **必须清零之后才能开始真实实验**，否则跑出来的数字全是错的。
    """
    outcome = run_once(task, OracleRunner(GOLD_PATCHES), image=image, tmp_path=tmp_path)

    assert outcome.verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert outcome.verdict.infra_outcome is InfraOutcome.SUCCESS
    assert outcome.verdict.agent_outcome is AgentOutcome.RESOLVED, outcome.verdict.reason
    assert outcome.verdict.f2p_ok and outcome.verdict.p2p_ok


@pytest.mark.parametrize("task", TASKS, ids=[t.task_id for t in TASKS])
def test_noop_sentinel_never_resolves(task: TaskDefinition, image: str, tmp_path: Path) -> None:
    """Noop 哨兵：空补丁绝不能判成修好。

    判成修好说明这道题在修复前测试就已经通过了 —— 那是坏题。
    """
    outcome = run_once(task, NoopRunner(), image=image, tmp_path=tmp_path)

    assert outcome.verdict.resolved is False, outcome.verdict.reason
    assert outcome.verdict.agent_outcome is AgentOutcome.EMPTY_PATCH
    assert outcome.verdict.f2p_ok is False
    # P2P 在修复前就该是通过的，空补丁不该把它们弄挂
    assert outcome.verdict.p2p_ok is True


# ── 全链路的证据都要留下来 ──────────────────────────────────


def test_artifacts_and_patches_are_stored(image: str, tmp_path: Path, store) -> None:
    """判定结论只有配上证据才有意义 —— 制品要真的落盘、真的读得回来。"""
    task = find(BEHAVIOR_TASK_ID)
    outcome = run_once(
        task, OracleRunner(GOLD_PATCHES), image=image, tmp_path=tmp_path, store=store
    )

    # 两份补丁都要存：只留标准化的，"AI 试图改测试文件"就再也查不到了（C-08b）
    assert set(outcome.patches) == {PatchKind.AGENT_RAW, PatchKind.AGENT_NORMALIZED}
    stored = store.get(outcome.patches[PatchKind.AGENT_NORMALIZED].key).decode("utf-8")
    assert stored == outcome.patch.text  # type: ignore[union-attr]

    # 日志类制品：Agent 摘要、测试容器输出、测试报告
    assert outcome.artifacts, "一个制品都没落盘"
    for ref in outcome.artifacts.values():
        assert store.exists(ref.key)
        assert store.get(ref.key)


def test_timings_are_recorded_in_order(image: str, tmp_path: Path) -> None:
    """各阶段时刻要按顺序记下来 —— 它们是耗时统计和超时归因的依据。"""
    task = find(BEHAVIOR_TASK_ID)
    t = run_once(task, OracleRunner(GOLD_PATCHES), image=image, tmp_path=tmp_path).timings

    assert t.prepare_started_at is not None
    assert t.agent_started_at is not None, "AI 跑过了，agent_started_at 不能为空（C-69）"
    assert t.agent_started_at <= t.agent_finished_at  # type: ignore[operator]
    assert t.test_started_at <= t.test_finished_at  # type: ignore[operator]
    assert t.judged_at <= t.completed_at  # type: ignore[operator]
    assert t.total_duration_ms is not None and t.total_duration_ms > 0


def test_per_case_results_cover_every_listed_test(image: str, tmp_path: Path) -> None:
    """题目列出的每一条用例都要有一条记录 —— "结论可查"的基础。"""
    task = find(BEHAVIOR_TASK_ID)
    outcome = run_once(task, OracleRunner(GOLD_PATCHES), image=image, tmp_path=tmp_path)

    recorded = {case.test_id for case in outcome.verdict.cases}
    for test_id in [*task.fail_to_pass, *task.pass_to_pass]:
        assert test_id in recorded, f"{test_id} 没有留下逐条记录"


# ── Mock 的几种行为 ─────────────────────────────────────────


def test_mock_that_edits_tests_is_caught(image: str, tmp_path: Path) -> None:
    """MockAgent 去改受保护路径 —— 改动被丢弃，而且留下"它试过"的证据。

    这一条把 E3-T3 的第一道防线（生成补丁时按路径过滤）和 E4-T2 的第二道
    （跑测试前强制还原）串起来验：结论是没修好，同时
    `protected_path_edit_attempted` 要为真 —— C-13d 要求它**本身**就触发人工复核。
    """
    task = find(BEHAVIOR_TASK_ID)
    runner = MockRunner(MockBehavior.PROTECTED_PATH_EDIT, protected_target="tests/test_csvline.py")
    outcome = run_once(task, runner, image=image, tmp_path=tmp_path)

    assert outcome.verdict.resolved is False
    assert outcome.patch is not None
    assert outcome.patch.protected_path_edit_attempted is True
    # 受保护那个文件被丢掉了
    assert "tests/test_csvline.py" not in outcome.patch.text
    # 但它同时新建的那个普通源文件必须留着 —— 挡过头就是把 AI 的修复删了（C-63a）
    assert "mock_agent_attempt.py" in outcome.patch.text
    # C-08a：过滤前不是空的
    assert outcome.patch.raw_patch_empty is False


def test_mock_with_an_unapplicable_patch_is_invalid_patch(image: str, tmp_path: Path) -> None:
    """补丁打不上 → `INVALID_PATCH`，责任在 AI，但**不算平台故障**。"""
    task = find(BEHAVIOR_TASK_ID)
    outcome = run_once(
        task, MockRunner(MockBehavior.MALFORMED_PATCH), image=image, tmp_path=tmp_path
    )

    assert outcome.verdict.infra_outcome is InfraOutcome.PATCH_APPLY_FAILED
    assert outcome.verdict.agent_outcome is AgentOutcome.INVALID_PATCH
    assert outcome.verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert outcome.verdict.counts_as_infra_failure is False


def test_mock_that_times_out_is_the_agents_fault(image: str, tmp_path: Path) -> None:
    """适配器报超时 → 责任在 AI，判 `UNRESOLVED`，`lifecycle` 仍是 `COMPLETED`。

    这条最容易写反 —— 判成 `FAILED` 的话，AI 只要把自己搞崩（或者拖到超时）
    就能从解决率的分母里消失，越不稳定的 AI 分数越好看。

    C-09a 还要求：超时时**仍然保存已改出来的补丁**（供失败分析用），但不跑测试。
    """
    task = find(BEHAVIOR_TASK_ID)
    outcome = run_once(task, MockRunner(MockBehavior.TIMEOUT), image=image, tmp_path=tmp_path)

    assert outcome.verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert outcome.verdict.infra_outcome is InfraOutcome.AGENT_TIMEOUT
    assert outcome.verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert outcome.verdict.counts_as_infra_failure is False
    assert outcome.patch is not None, "C-09a：超时也要把补丁留下来"
    assert outcome.execution is None, "超时不跑测试"


def test_mock_with_a_wrong_patch_is_unresolved(image: str, tmp_path: Path) -> None:
    """补丁能打上但没修对 → `UNRESOLVED`，F2P 仍然挂着。"""
    task = find(BEHAVIOR_TASK_ID)
    outcome = run_once(task, MockRunner(MockBehavior.WRONG_PATCH), image=image, tmp_path=tmp_path)

    assert outcome.verdict.infra_outcome is InfraOutcome.SUCCESS
    assert outcome.verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert outcome.verdict.f2p_ok is False


# ── 确定性哨兵 ──────────────────────────────────────────────


def test_same_agent_gives_identical_verdicts_three_times(image: str, tmp_path: Path) -> None:
    """同一个补丁跑 3 次，每条用例的状态必须完全一致（`AGENTS.md` §9 哨兵 3）。

    整条链路（物化 → 打补丁 → 起容器 → 跑 pytest → 解析 → 判定）跑三遍，
    任何一环有随机性都会在这里露出来。
    """
    task = find(BEHAVIOR_TASK_ID)
    runs = [
        run_once(task, OracleRunner(GOLD_PATCHES), image=image, tmp_path=tmp_path, name=f"det{i}")
        for i in range(3)
    ]

    statuses = [
        json.dumps(
            sorted((c.test_id, c.role.value, c.status.value) for c in run.verdict.cases),
            ensure_ascii=False,
        )
        for run in runs
    ]
    assert statuses[0] == statuses[1] == statuses[2]
    outcomes = {run.verdict.agent_outcome for run in runs}
    assert len(outcomes) == 1


# ── 平台故障不能冒泡成异常 ──────────────────────────────────


def test_missing_mirror_becomes_workspace_error(image: str, tmp_path: Path) -> None:
    """镜像仓库不存在 → `WORKSPACE_ERROR` + `NOT_ATTEMPTED`，**不抛异常**。

    让异常冒出去的话，这条记录会停在非终态，永远不会被判定，也不会进任何统计 ——
    它只是消失了，而且没人会发现。
    """
    task = find(BEHAVIOR_TASK_ID)
    outcome = execute_task_run(
        OracleRunner(GOLD_PATCHES),
        TaskRunInputs(
            plan=task.execution_plan(),
            agent_input=task.agent_task_input(deadline_unix_ms=deadline_ms(60)),
            mirror_path=tmp_path / "no-such-mirror.git",
            scratch_dir=tmp_path / "ws",
            image=image,
        ),
    )

    assert outcome.verdict.infra_outcome is InfraOutcome.WORKSPACE_ERROR
    assert outcome.verdict.lifecycle_status is LifecycleStatus.FAILED
    assert outcome.verdict.agent_outcome is AgentOutcome.NOT_ATTEMPTED
    assert outcome.verdict.counts_as_infra_failure is True
    assert outcome.timings.agent_started_at is None, "AI 从未启动（C-69 的判据）"
