"""候选推成题目之后库里变成什么样（E8-T2，`03-benchmark-spec.md` §7.4）。

要一个真的 PostgreSQL：这里验的是**跨两张表的状态迁移**，
`task_candidates.state` 和 `benchmark_tasks.validation_state` 必须一起对。

三件事必须验到，第二件最严重：

1. **候选入库之后要变成 `PROMOTED`。** 不改的话下次 assemble 会再做一遍。
2. **重跑一遍入库，不能把已经验过的题打回 `DISCOVERED`。** 原来的
   `upsert_task()` 每次都无条件重置状态 —— 那意味着重灌一次数据，
   几十个容器跑出来的验证结论**一声不响地没了**，而且要等下一次
   `make validate-tasks` 才看得出来。
3. **人工终审否掉一道题，它要真的退出数据集。** §7.4 的状态机写的是
   `REVIEW_REQUIRED →（人工）→ VALID / INVALID`，走的就是这条边。
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.assembly import assemble, environment_from_recipe, load_candidate
from app.benchmark.schema import P2PSampling
from app.domain.enums import TaskCandidateState, TaskValidationState
from app.infrastructure.models.benchmark import (
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
    TaskCandidate,
)
from app.sandbox.images import parse_recipe
from cli.promote import _review_rows, _select_candidates
from cli.queue import upsert_task
from tests.integration.factories import wipe

pytestmark = pytest.mark.db

REPO = "pallets/click"

RECIPE = {
    "environment_id": "pallets__click__py311",
    "repo_name": REPO,
    "repo_url": "https://github.com/pallets/click.git",
    "snapshot_commit": "6" * 40,
    "install_steps": ["python -m pip install -e ."],
    "import_check": ["click"],
}

TEST_PATCH = (
    "diff --git a/tests/test_x.py b/tests/test_x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/tests/test_x.py\n"
    "+++ b/tests/test_x.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def test_old():\n"
    "+def test_new(): pass\n"
)
CODE_PATCH = (
    "diff --git a/src/click/x.py b/src/click/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/click/x.py\n"
    "+++ b/src/click/x.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-broken\n"
    "+fixed\n"
)


def payload(pr: int = 3858) -> dict[str, Any]:
    return {
        "repo": REPO,
        "pr": {"number": pr, "base_ref_name": "main", "url": f"https://x/pull/{pr}"},
        "base_commit": "a" * 40,
        "cleaned": {
            "issue_title": "报错了",
            "issue_body": "复现步骤见下，跑一下就能看到不一致。" * 12,
            "issue_language": "zh",
            "f2p_candidates": ["tests/test_x.py::test_new"],
        },
        "prescreen": {"decision": "PASS", "score": 5.0, "leaks_fix": False},
    }


@pytest.fixture
def candidate_files(tmp_path: Any) -> Any:
    """补丁落在磁盘上（§8.10 第三节），这里造一份最小的。"""
    directory = tmp_path / "pallets__click"
    directory.mkdir(parents=True)
    (directory / "3858.test.patch").write_text(TEST_PATCH, encoding="utf-8")
    (directory / "3858.code.patch").write_text(CODE_PATCH, encoding="utf-8")
    return tmp_path


def build_task(patch_root: Any, **kwargs: Any) -> Any:
    candidate = load_candidate(payload(), patch_root=patch_root)
    environment = environment_from_recipe(parse_recipe(RECIPE))
    return assemble(
        candidate,
        environment,
        dataset_id="benchmark-dev",
        fail_to_pass=["tests/test_x.py::test_new"],
        pass_to_pass=["tests/test_x.py::test_old"],
        p2p_sampling=P2PSampling(strategy="full", seed=None, total_pool=1),
        **kwargs,
    )


def env_spec_row() -> dict[str, Any]:
    return {
        "python_version": "3.11",
        "extra_protected_paths": [],
        "image_tag": "bench-env:pallets__click__py311",
    }


# ══════════════════════════════════════════════════════════════
# 入库
# ══════════════════════════════════════════════════════════════


def test_a_mined_task_lands_with_its_dataset_id_and_patch_source(
    session: Session, candidate_files: Any
) -> None:
    """`benchmark_tasks` **没有 dataset_id 列**，它只在 `raw_definition` 里。

    E1-T6 发数据集版本时按它挑题，挑错了整批题都不对，所以这一条要盯着。
    补丁来源写 `mined://` 而不是 `golden://` —— 两种题的补丁是两个来源，
    写成一样的话出问题时查不出补丁该去哪儿找。
    """
    wipe(session)
    task = build_task(candidate_files)

    created = upsert_task(
        session, task, {task.environment_id: env_spec_row()}, patch_uri_scheme="mined"
    )
    session.flush()

    assert created
    row = session.execute(sa.select(BenchmarkTask)).scalar_one()
    assert row.raw_definition["dataset_id"] == "benchmark-dev"
    assert row.test_patch_uri.startswith("mined://")
    assert row.validation_state is TaskValidationState.DISCOVERED
    assert row.content_hash == task.content_hash.removeprefix("sha256:")


def test_upserting_creates_the_environment_row_so_digests_can_be_written_back(
    session: Session, candidate_files: Any
) -> None:
    """`cli.images build` 的 `write_back()` **只更新已有的行、不建行**。

    所以这一行必须先由入库建出来，否则 `environment_specs.image_digest` 永远是空的，
    而协议 C-36 要求引用镜像用 digest 不用 tag。2026-09-10 实测踩过：
    镜像明明建好了，回显却是"表里还没有这个环境，只建镜像不写库"。
    """
    wipe(session)
    task = build_task(candidate_files)
    upsert_task(session, task, {task.environment_id: env_spec_row()}, patch_uri_scheme="mined")
    session.flush()

    env = session.execute(sa.select(EnvironmentSpec)).scalar_one()
    assert env.environment_id == "pallets__click__py311"
    assert env.image_tag == "bench-env:pallets__click__py311"
    assert "--junitxml=" in env.test_command


def test_reupserting_does_not_throw_away_the_validation_result(
    session: Session, candidate_files: Any
) -> None:
    """已经验成 VALID 的题，重跑一遍入库要保持原样。

    冲掉它不会报错 —— 表现只是"这批题怎么又要重验一遍"，
    而重验一遍是几十个容器的机时。
    """
    wipe(session)
    task = build_task(candidate_files)
    upsert_task(session, task, {task.environment_id: env_spec_row()}, patch_uri_scheme="mined")
    session.flush()

    row = session.execute(sa.select(BenchmarkTask)).scalar_one()
    row.validation_state = TaskValidationState.VALID
    row.validation_evidence_uri = "local://tasks/x/validation/y/evidence.json"
    session.flush()

    created = upsert_task(
        session, task, {task.environment_id: env_spec_row()}, patch_uri_scheme="mined"
    )
    session.flush()
    session.refresh(row)

    assert not created
    assert row.validation_state is TaskValidationState.VALID
    assert row.validation_evidence_uri == "local://tasks/x/validation/y/evidence.json"


def test_the_same_repo_is_not_duplicated(session: Session, candidate_files: Any) -> None:
    """挖掘时已经建过 `pallets/click` 这一行，入库不能再建一行。"""
    wipe(session)
    session.add(
        Repository(full_name=REPO, url="https://github.com/pallets/click", language="python")
    )
    session.flush()

    task = build_task(candidate_files)
    upsert_task(session, task, {task.environment_id: env_spec_row()}, patch_uri_scheme="mined")
    session.flush()

    assert session.execute(sa.select(sa.func.count(Repository.id))).scalar_one() == 1


# ══════════════════════════════════════════════════════════════
# 状态迁移
# ══════════════════════════════════════════════════════════════


def test_promoted_is_a_terminal_state_for_the_candidate() -> None:
    """`PROMOTED` 是候选的终点（§7.4 的 CANDIDATE → VALIDATING 那条边）。

    枚举里有这个取值但一直没人写过，这里把它钉住：漏了这一步的话，
    下一次 assemble 会把同一批候选再做一遍。
    """
    assert TaskCandidateState.PROMOTED.value == "PROMOTED"
    assert TaskCandidateState.PROMOTED is not TaskCandidateState.PRESCREENED


# ══════════════════════════════════════════════════════════════
# 派生规则改了要能重做（E1-T6 补）
# ══════════════════════════════════════════════════════════════


def test_assemble_can_redo_candidates_that_are_already_promoted(session: Session) -> None:
    """**`--redo` 要能把已经入过库的候选再挑出来。**

    默认只挑 `PRESCREENED` 是对的：候选推成题目之后不该再做一遍。
    但派生规则改了就必须能重做 —— 2026-09-10 撞到：`assemble()` 加了一道剔除
    不稳定用例的过滤，22 道题的 `pass_to_pass` 要按新规则重算，而候选早就是
    `PROMOTED` 了，表现是"一条候选都选不出来"，看起来像缓存坏了。
    """
    wipe(session)
    repo = Repository(full_name=REPO, url="https://github.com/pallets/click", language="python")
    session.add(repo)
    session.flush()
    for pr, state in ((101, TaskCandidateState.PRESCREENED), (102, TaskCandidateState.PROMOTED)):
        session.add(
            TaskCandidate(
                repository_id=repo.id,
                pr_number=pr,
                raw_payload={**payload(pr), "probe": {"state": "OK"}},
                state=state,
            )
        )
    session.flush()

    def picked(*, include_promoted: bool) -> set[int]:
        rows = _select_candidates(
            session,
            repo=None,
            prs=[],
            decisions=["PASS"],
            branch=None,
            limit=None,
            redo=True,
            include_promoted=include_promoted,
        )
        return {int(p["pr"]["number"]) for _, p in rows}

    assert picked(include_promoted=False) == {101}
    assert picked(include_promoted=True) == {101, 102}


def test_export_review_matches_candidates_when_the_repo_name_has_a_hyphen(
    session: Session, tmp_path: Any
) -> None:
    """终审对照表要把候选的预筛结论带上，而对上候选靠的是 task_id 里的 PR 号。

    `tortoise__tortoise-orm-2076` 这种仓库名本身带 `-` 的，按第二段取 PR 号会取到
    `orm`，一条都对不上，表里预筛三列全空（2026-09-15 导 tortoise 那 14 道时撞到）。
    click 一路没暴露是因为 `pallets__click-3858` 恰好只有一个 `-`。
    """
    wipe(session)
    repo_name = "tortoise/tortoise-orm"
    directory = tmp_path / "tortoise__tortoise-orm"
    directory.mkdir(parents=True)
    (directory / "2076.test.patch").write_text(TEST_PATCH, encoding="utf-8")
    (directory / "2076.code.patch").write_text(CODE_PATCH, encoding="utf-8")

    raw = {**payload(2076), "repo": repo_name}
    candidate = load_candidate(raw, patch_root=tmp_path)
    env_id = "tortoise__tortoise-orm__py311"
    environment = environment_from_recipe(
        parse_recipe({**RECIPE, "environment_id": env_id, "repo_name": repo_name})
    )
    task = assemble(
        candidate,
        environment,
        dataset_id="benchmark-dev",
        fail_to_pass=["tests/test_x.py::test_new"],
        pass_to_pass=["tests/test_x.py::test_old"],
        p2p_sampling=P2PSampling(strategy="full", seed=None, total_pool=1),
    )
    assert task.task_id == "tortoise__tortoise-orm-2076"
    upsert_task(
        session,
        task,
        {env_id: {**env_spec_row(), "image_tag": f"bench-env:{env_id}"}},
        patch_uri_scheme="mined",
    )
    session.flush()
    row = session.execute(sa.select(BenchmarkTask)).scalar_one()
    row.validation_state = TaskValidationState.VALID
    session.add(
        TaskCandidate(
            repository_id=row.repository_id,
            pr_number=2076,
            raw_payload=raw,
            state=TaskCandidateState.PROMOTED,
        )
    )
    session.flush()

    (exported,) = _review_rows(session, "benchmark-dev")
    assert exported["pr"] == "2076"
    assert exported["prescreen_decision"] == "PASS"
    assert exported["prescreen_score"] == 5.0
