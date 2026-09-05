"""测试执行器的端到端验收（E4-T2）。

**需要 Docker，也需要 `make images` 建好的测试镜像**，所以整个模块打 docker 标记，
`make test` 会跳过，`make test-docker` 才跑。

跑的是真实的 Golden 题（textkit），真起容器，真跑 pytest。这一层要证明三件事：

1. **判定链是通的** —— 空补丁 F2P 全挂、官方补丁全过（Noop 0% / Oracle 100% 的依据）。
2. **改测试没用**（本任务的 AC）—— 被测 AI 把测试文件改成永远通过、或者新塞一个
   `conftest.py`，F2P 照样挂。
3. **故障分类不冤枉人** —— 补丁打不上算 `PATCH_APPLY_FAILED`，测试补丁打不上算
   `TEST_DISCOVERY_ERROR`（题目坏了），两者都不是"AI 没修好"。

## 为什么要在真容器里再验一遍

`test_protected_restore.py` 已经用纯 git 验过强制还原了，那层快得多。但那层证明的是
"文件被还原了"，证明不了"所以 pytest 真的跑的是官方测试"。中间还隔着
打测试补丁、容器挂载、报告落盘、ID 归一化四步 —— 任何一步接错，还原本身是对的，
结论仍然是错的。防作弊这种事，只信端到端的证据。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

# TestStatus 起别名：直接 import 会被 pytest 当成待收集的测试类，多一条告警。
from app.domain.enums import InfraOutcome
from app.domain.enums import TestStatus as Status
from app.domain.execution_plan import ExecutionPlan
from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE, execute_tests
from app.sandbox.container import DockerUnavailableError, get_docker_client
from app.sandbox.git_cli import run_git
from app.sandbox.workspace import materialize_workspace

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_TASK = REPO_ROOT / "datasets" / "golden" / "bench-golden__textkit-1.json"
GOLDEN_MIRROR = REPO_ROOT / "var" / "mirrors" / "bench-golden__textkit.git"

#: 作弊用的测试文件内容：把每条 F2P 都改成永远通过。
#: 真实的作弊补丁不会写得这么直白，但效果是一样的，而这样写谁都看得懂它在干什么。
CHEATING_TESTS = """from textkit.csvline import parse_line


def test_splits_plain_fields():
    assert True


def test_keeps_empty_fields():
    assert True


def test_single_field():
    assert True


def test_strips_trailing_newline():
    assert True


def test_quoted_field_keeps_comma():
    assert True


def test_quoted_field_in_the_middle():
    assert True


def test_double_quote_inside_quoted_field():
    assert True
"""

#: 另一条作弊路子：不碰任何现有文件，新塞一个 conftest 把用例全部跳过。
#: `git checkout` 管不到未跟踪文件，专门用来验证 C-63 那一半。
CHEATING_CONFTEST = """import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="被 conftest 跳过了"))
"""


@pytest.fixture(scope="module")
def image() -> str:
    """确认测试镜像在本机。不在就跳过，并说清楚怎么建。"""
    try:
        client = get_docker_client()
    except DockerUnavailableError as exc:
        pytest.skip(f"Docker 不可用：{exc}")
    try:
        client.images.get(DEFAULT_GOLDEN_IMAGE)
    except Exception:
        pytest.skip(f"没有 {DEFAULT_GOLDEN_IMAGE}，先跑 `make images`")
    return DEFAULT_GOLDEN_IMAGE


@pytest.fixture(scope="module")
def task() -> dict:
    if not GOLDEN_MIRROR.exists():
        pytest.skip(f"没有镜像仓库 {GOLDEN_MIRROR}，先跑 `make golden`")
    return json.loads(GOLDEN_TASK.read_text(encoding="utf-8"))


@pytest.fixture
def plan(task: dict) -> ExecutionPlan:
    """从真实题目 JSON 建执行计划。

    这里手工建而不是走 `TaskDefinition.execution_plan()`：那条路在
    `tests/unit/test_execution_plan.py` 里单独验，这边只关心执行器本身。
    """
    return ExecutionPlan(
        base_commit=task["base_commit"],
        test_patch=task["test_patch"],
        test_patch_paths=tuple(task["test_patch_paths"]),
        fail_to_pass=tuple(task["fail_to_pass"]),
        pass_to_pass=tuple(task["pass_to_pass"]),
        test_command=task["test_command"],
        test_report_path=task["test_report_path"],
        test_timeout_s=60,
        task_id=task["task_id"],
    )


@pytest.fixture
def make_patch(task: dict, tmp_path: Path) -> Callable[[Callable[[Path], None]], str]:
    """造一段"AI 交上来的补丁"。

    做法是物化一个临时工作区、改动它、再 `git diff` —— 和真实评测里补丁的来路
    （E3-T3 的 `capture_workspace_diff`）一致。手写 diff 的话，
    上下文行数一错就打不上，而那种失败看起来像执行器有 bug。
    """
    counter = {"n": 0}

    def build(mutate: Callable[[Path], None]) -> str:
        counter["n"] += 1
        workspace = materialize_workspace(
            mirror_path=GOLDEN_MIRROR,
            base_commit=task["base_commit"],
            dest=tmp_path / f"scratch{counter['n']}",
        )
        mutate(workspace.path)
        run_git(["add", "--all"], cwd=workspace.path)
        diff = run_git(["diff", "--cached", workspace.base_sha], cwd=workspace.path).stdout
        return diff + "\n" if diff else ""

    return build


def run(plan: ExecutionPlan, patch: str, image: str, tmp_path: Path, name: str = "ws"):
    return execute_tests(
        plan, patch, mirror_path=GOLDEN_MIRROR, workspace_dir=tmp_path / name, image=image
    )


def statuses(outcome, ids: tuple[str, ...]) -> list[Status | None]:
    assert outcome.report is not None
    return [c.status if (c := outcome.report.resolve(t)) else None for t in ids]


# ── 判定链本身 ──────────────────────────────────────────────


def test_empty_patch_leaves_f2p_failing(plan: ExecutionPlan, image: str, tmp_path: Path) -> None:
    """空补丁：F2P 全挂、P2P 全过。这就是 Noop 哨兵解决率 0% 的依据。"""
    outcome = run(plan, "", image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.SUCCESS
    assert statuses(outcome, plan.fail_to_pass) == [Status.FAILED] * len(plan.fail_to_pass)
    assert statuses(outcome, plan.pass_to_pass) == [Status.PASSED] * len(plan.pass_to_pass)
    assert outcome.restore.attempted is False


def test_gold_patch_resolves_everything(
    plan: ExecutionPlan, task: dict, image: str, tmp_path: Path
) -> None:
    """官方补丁：F2P 和 P2P 全过。这是 Oracle 哨兵解决率 100% 的依据。"""
    outcome = run(plan, task["gold_patch"], image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.SUCCESS
    expected = [Status.PASSED] * len(plan.test_ids)
    assert statuses(outcome, plan.test_ids) == expected


def test_only_requested_ids_are_run(plan: ExecutionPlan, image: str, tmp_path: Path) -> None:
    """只跑 F2P + P2P 列的用例，不跑全量（C-17）。"""
    outcome = run(plan, "", image, tmp_path)

    assert outcome.report is not None
    assert set(outcome.report.cases) == set(plan.test_ids)


# ── AC：改测试没用 ──────────────────────────────────────────


def test_rewriting_tests_does_not_help(
    plan: ExecutionPlan, image: str, tmp_path: Path, make_patch
) -> None:
    """**本任务的 AC。** AI 把测试文件整个换成 `assert True`，F2P 照样全挂。

    喂进去的是**没经过 E3-T3 过滤的原始补丁** —— 故意的。C-16 要求第二道防线
    单独成立：就算第一道（生成补丁时按路径过滤）整个失效，这一道也得挡住。
    """
    cheat = make_patch(
        lambda root: (root / "tests" / "test_csvline.py").write_text(CHEATING_TESTS, "utf-8")
    )
    assert "tests/test_csvline.py" in cheat, "作弊补丁没碰测试文件，这条用例就白测了"

    outcome = run(plan, cheat, image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.SUCCESS
    assert statuses(outcome, plan.fail_to_pass) == [Status.FAILED] * len(plan.fail_to_pass)
    # 而且要留下"我们确实挡住了"的证据
    assert outcome.restore.attempted is True
    assert "tests/test_csvline.py" in outcome.restore.restored


def test_added_conftest_does_not_help(
    plan: ExecutionPlan, image: str, tmp_path: Path, make_patch
) -> None:
    """AI 新塞一个 `conftest.py` 把用例全部跳过 —— 也没用（C-63）。

    这条走的是另一半防线：`git checkout` 只管已跟踪的文件，删不掉新建的未跟踪文件。
    真被跳过的话状态会是 SKIPPED，而 C-12 禁止把 SKIPPED 当通过，
    所以就算漏了这一半，判定也不会把它算成修好 —— 但那时 F2P 会变成 SKIPPED 而不是
    FAILED，归因就全乱了。这里断言的是 FAILED。
    """
    cheat = make_patch(
        lambda root: (root / "conftest.py").write_text(CHEATING_CONFTEST, encoding="utf-8")
    )

    outcome = run(plan, cheat, image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.SUCCESS
    assert statuses(outcome, plan.fail_to_pass) == [Status.FAILED] * len(plan.fail_to_pass)
    assert outcome.restore.deleted == ("conftest.py",)


def test_deleting_the_test_file_does_not_help(
    plan: ExecutionPlan, image: str, tmp_path: Path, make_patch
) -> None:
    """AI 直接把测试文件删了 —— 还原之后照样跑得起来。

    不还原的话 pytest 收集不到用例，报告里全是 MISSING，看起来像我们的解析器坏了。
    """
    cheat = make_patch(lambda root: (root / "tests" / "test_csvline.py").unlink())

    outcome = run(plan, cheat, image, tmp_path)

    assert statuses(outcome, plan.fail_to_pass) == [Status.FAILED] * len(plan.fail_to_pass)
    assert statuses(outcome, plan.pass_to_pass) == [Status.PASSED] * len(plan.pass_to_pass)


def test_legit_new_source_file_survives(
    plan: ExecutionPlan, task: dict, image: str, tmp_path: Path, make_patch
) -> None:
    """AI 新建源码文件 + 正确修复 —— 新文件必须留着，题目要判成修好（C-63a）。

    反过来的错误（把新增源文件也删掉）不会报任何错，只会让解决率偏低，
    而且看起来像"这个 AI 就是修不对"。
    """

    def mutate(root: Path) -> None:
        (root / "textkit" / "helper.py").write_text("MARKER = 'kept'\n", encoding="utf-8")

    cheat = make_patch(mutate)
    outcome = run(plan, task["gold_patch"] + cheat, image, tmp_path)

    assert outcome.restore.deleted == ()
    assert statuses(outcome, plan.test_ids) == [Status.PASSED] * len(plan.test_ids)


# ── 故障分类 ────────────────────────────────────────────────


def test_unapplicable_patch_is_patch_apply_failed(
    plan: ExecutionPlan, image: str, tmp_path: Path
) -> None:
    """AI 的补丁打不上 → `PATCH_APPLY_FAILED`，不是"没修好"。"""
    broken = (
        "diff --git a/nope/missing.py b/nope/missing.py\n"
        "--- a/nope/missing.py\n"
        "+++ b/nope/missing.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    outcome = run(plan, broken, image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.PATCH_APPLY_FAILED
    assert outcome.report is None
    assert outcome.container is None, "补丁都没打上，不该起容器"


def test_broken_test_patch_is_test_discovery_error(
    plan: ExecutionPlan, image: str, tmp_path: Path
) -> None:
    """官方测试补丁打不上 → 题目坏了（`TEST_DISCOVERY_ERROR`），也不是 AI 的锅。"""
    outcome = run(replace(plan, test_patch="这根本不是一段 diff\n"), "", image, tmp_path)

    assert outcome.infra_outcome is InfraOutcome.TEST_DISCOVERY_ERROR
    assert outcome.report is None


# ── 确定性哨兵 ──────────────────────────────────────────────


def test_same_patch_gives_identical_results(
    plan: ExecutionPlan, task: dict, image: str, tmp_path: Path
) -> None:
    """同一个补丁跑两次，每条用例的状态必须完全一致（`AGENTS.md` §9 哨兵 3）。

    判定有随机性的话，这个月的排行榜和下个月的排行榜就没法放在一起看。
    """
    first = run(plan, task["gold_patch"], image, tmp_path, name="ws-a")
    second = run(plan, task["gold_patch"], image, tmp_path, name="ws-b")

    assert first.infra_outcome is second.infra_outcome
    assert first.report is not None and second.report is not None
    assert first.report.statuses == second.report.statuses
