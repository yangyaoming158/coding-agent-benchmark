"""挖掘结果怎么落库（E1-T4，`03-benchmark-spec.md` §8.4）。

要一个真的 PostgreSQL：这里验的是 `ON CONFLICT` 的行为，SQLite 上不成立，
而 `task_candidates` 的 `UNIQUE(repository_id, pr_number)` 正是
**"重复运行不产生重复候选"这条 AC 的全部实现**。

两件事必须验到，第二件比第一件严重：

1. **同一批候选灌两遍，表里行数不变。** 这是 AC 的字面要求。
2. **已经被 E1-T5 打过分的行，重挖一遍不能被冲掉。** 挖掘是可以随便重跑的
   （限流、续跑、换时间窗），而预筛分数背后有人工核对（E1-T5 的 AC 要求
   抽 20 条人工核对一致率 ≥80%）。重跑一次把它冲成 `DISCOVERED`，
   那些核对就白做了，而且**不会报错**。
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.mining import MinedPR, parse_pr_node
from app.domain.enums import TaskCandidateState
from app.infrastructure.models.benchmark import Repository, TaskCandidate
from cli.mine import upsert_candidates, upsert_repository
from tests.integration.factories import wipe

pytestmark = pytest.mark.db

REPO = "pallets/click"

#: `gh api repos/pallets/click` 返回里我们用到的那几个字段。
REPO_META: dict[str, Any] = {
    "html_url": "https://github.com/pallets/click",
    "default_branch": "main",
    "language": "Python",
    "stargazers_count": 16000,
    "license": {"spdx_id": "BSD-3-Clause"},
}


def make_pr(number: int, *, issue: int = 100, paths: tuple[str, ...] = ()) -> MinedPR:
    """造一条合格的候选。形状抄自真实 GraphQL 返回。"""
    file_paths = paths or ("tests/test_types.py", "src/click/types.py")
    pr = parse_pr_node(
        {
            "number": number,
            "title": f"fix something #{number}",
            "url": f"https://github.com/{REPO}/pull/{number}",
            "createdAt": "2025-05-25T21:36:12Z",
            "mergedAt": "2025-05-26T20:17:50Z",
            "baseRefName": "main",
            "baseRefOid": "a" * 40,
            "additions": 6,
            "deletions": 1,
            "changedFiles": len(file_paths),
            "commits": {"totalCount": 1},
            "mergeCommit": {
                "oid": "c" * 40,
                "parents": {"totalCount": 2, "nodes": [{"oid": "a" * 40}, {"oid": "b" * 40}]},
            },
            "author": {"login": "someone"},
            "files": {
                "totalCount": len(file_paths),
                "nodes": [{"path": p} for p in file_paths],
            },
            "closingIssuesReferences": {
                "totalCount": 1,
                "nodes": [
                    {
                        "number": issue,
                        "title": "报错了",
                        "body": "复现步骤见下",
                        "url": f"https://github.com/{REPO}/issues/{issue}",
                    }
                ],
            },
        }
    )
    assert pr is not None
    return pr


@pytest.fixture
def repo_id(session: Session) -> int:
    wipe(session)
    return upsert_repository(session, REPO, REPO_META, is_domestic=False).id


def count(session: Session, repository_id: int) -> int:
    return int(
        session.execute(
            sa.select(sa.func.count(TaskCandidate.id)).where(
                TaskCandidate.repository_id == repository_id
            )
        ).scalar_one()
    )


# ══════════════════════════════════════════════════════════════
# AC：重复运行不产生重复候选
# ══════════════════════════════════════════════════════════════


def test_mining_the_same_prs_twice_does_not_duplicate_rows(session: Session, repo_id: int) -> None:
    """AC 的字面要求。靠 `UNIQUE(repository_id, pr_number)`，不靠"我记得挖过了"。"""
    prs = [make_pr(n) for n in (2946, 2933, 2901)]

    first = upsert_candidates(session, repo_id, prs, repo=REPO)
    session.flush()
    assert count(session, repo_id) == 3
    assert (first.inserted, first.updated, first.left_alone) == (3, 0, 0)

    second = upsert_candidates(session, repo_id, prs, repo=REPO)
    session.flush()
    assert count(session, repo_id) == 3, "重挖一遍不该多出行来"
    assert (second.inserted, second.updated, second.left_alone) == (0, 3, 0)


def test_overlapping_batches_only_add_the_new_prs(session: Session, repo_id: int) -> None:
    """两个时间窗边界上重叠一两个 PR 是常态（窗口二分之后尤其如此）。"""
    upsert_candidates(session, repo_id, [make_pr(1), make_pr(2)], repo=REPO)
    session.flush()
    counts = upsert_candidates(session, repo_id, [make_pr(2), make_pr(3)], repo=REPO)
    session.flush()

    assert count(session, repo_id) == 3
    assert (counts.inserted, counts.updated) == (1, 1)


def test_the_same_pr_number_in_two_repos_is_two_candidates(session: Session) -> None:
    """唯一约束是 `(repository_id, pr_number)`，不是光看 PR 号 ——
    每个仓库的 PR 都从 1 开始编号。
    """
    wipe(session)
    click_id = upsert_repository(session, REPO, REPO_META, is_domestic=False).id
    sqlfluff_id = upsert_repository(session, "sqlfluff/sqlfluff", REPO_META, is_domestic=False).id

    upsert_candidates(session, click_id, [make_pr(42)], repo=REPO)
    upsert_candidates(session, sqlfluff_id, [make_pr(42)], repo="sqlfluff/sqlfluff")
    session.flush()

    assert count(session, click_id) == 1
    assert count(session, sqlfluff_id) == 1


# ══════════════════════════════════════════════════════════════
# 不许冲掉 E1-T5 的工作
# ══════════════════════════════════════════════════════════════


def test_rerunning_does_not_reset_a_prescreened_candidate(session: Session, repo_id: int) -> None:
    """E1-T5 打过分的行，重挖一遍原样不动。

    冲掉它不会报错 —— 表现只是"预筛结果莫名其妙没了、要重跑一遍 LLM"，
    而 LLM 预筛是要花钱的，人工核对更贵。
    """
    upsert_candidates(session, repo_id, [make_pr(2946)], repo=REPO)
    session.flush()

    row = session.execute(
        sa.select(TaskCandidate).where(TaskCandidate.pr_number == 2946)
    ).scalar_one()
    row.state = TaskCandidateState.PRESCREENED
    row.prescreen_score = 4
    row.prescreen_reason = "issue 自足、没有泄题"
    session.flush()

    counts = upsert_candidates(session, repo_id, [make_pr(2946, issue=999)], repo=REPO)
    session.flush()
    session.refresh(row)

    assert counts.left_alone == 1
    assert row.state is TaskCandidateState.PRESCREENED
    assert row.prescreen_score == 4
    assert row.prescreen_reason == "issue 自足、没有泄题"
    assert row.issue_number == 100, "还没被处理时才更新 issue_number"


def test_rerunning_refreshes_a_candidate_that_is_still_untouched(
    session: Session, repo_id: int
) -> None:
    """还是 `DISCOVERED` 的行可以更新：GitHub 上 issue 被人事后编辑过就该跟上。"""
    upsert_candidates(session, repo_id, [make_pr(2946, issue=100)], repo=REPO)
    session.flush()
    upsert_candidates(session, repo_id, [make_pr(2946, issue=777)], repo=REPO)
    session.flush()

    row = session.execute(
        sa.select(TaskCandidate).where(TaskCandidate.pr_number == 2946)
    ).scalar_one()
    assert row.issue_number == 777


# ══════════════════════════════════════════════════════════════
# 落库的内容
# ══════════════════════════════════════════════════════════════


def test_a_fresh_candidate_lands_as_discovered_with_nothing_prescreened(
    session: Session, repo_id: int
) -> None:
    """E1-T4 的产出就是 `DISCOVERED`。预筛那几列留空，那是 E1-T5 的活。

    §7.4 图里的 `CANDIDATE` 指的是"进了 task_candidates 这张表"，
    不是一个状态值 —— 库里的枚举只有 DISCOVERED / PRESCREENED / PROMOTED / REJECTED。
    """
    upsert_candidates(session, repo_id, [make_pr(2946)], repo=REPO)
    session.flush()

    row = session.execute(sa.select(TaskCandidate)).scalar_one()
    assert row.state is TaskCandidateState.DISCOVERED
    assert row.prescreen_score is None
    assert row.prescreen_reason is None
    assert row.reject_reason is None


def test_raw_payload_keeps_the_issue_body_unredacted(session: Session, repo_id: int) -> None:
    """**这一步只抄，不改。** 脱敏是 E1-T5 的活，它要拿原文来核对自己脱干净了没有。"""
    upsert_candidates(session, repo_id, [make_pr(2946)], repo=REPO)
    session.flush()

    payload = session.execute(sa.select(TaskCandidate.raw_payload)).scalar_one()
    assert payload["issues"][0]["body"] == "复现步骤见下"
    assert payload["base_commit"] == "a" * 40
    assert payload["files"]["test_paths"] == ["tests/test_types.py"]


def test_empty_batch_touches_nothing(session: Session, repo_id: int) -> None:
    """一页里一条候选都没有是常态（大多数 merged PR 不带测试）。"""
    counts = upsert_candidates(session, repo_id, [], repo=REPO)
    assert (counts.inserted, counts.updated, counts.left_alone) == (0, 0, 0)
    assert count(session, repo_id) == 0


# ══════════════════════════════════════════════════════════════
# 仓库行
# ══════════════════════════════════════════════════════════════


def test_upsert_repository_creates_then_updates(session: Session) -> None:
    """`task_candidates.repository_id` 是外键，挖之前必须有这一行。"""
    wipe(session)
    created = upsert_repository(session, REPO, REPO_META, is_domestic=False)
    assert created.stars == 16000
    assert created.license == "BSD-3-Clause"
    assert created.default_branch == "main"

    again = upsert_repository(
        session, REPO, {**REPO_META, "stargazers_count": 16500}, is_domestic=True
    )
    assert again.id == created.id
    assert again.stars == 16500
    assert again.is_domestic is True
    assert session.execute(sa.select(sa.func.count(Repository.id))).scalar_one() == 1


def test_upsert_repository_leaves_the_mirror_path_alone(session: Session) -> None:
    """`mirror_path` 是 E2-T1 物化工作区时写的，挖掘不该碰它。"""
    wipe(session)
    repo = upsert_repository(session, REPO, REPO_META, is_domestic=False)
    repo.mirror_path = "var/mirrors/pallets__click.git"
    session.flush()

    upsert_repository(session, REPO, REPO_META, is_domestic=False)
    session.flush()
    assert repo.mirror_path == "var/mirrors/pallets__click.git"
