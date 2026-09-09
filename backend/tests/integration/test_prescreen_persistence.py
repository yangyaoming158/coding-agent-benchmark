"""清洗与预筛的结果怎么落库（E1-T5，`03-benchmark-spec.md` §8.4）。

要一个真的 PostgreSQL：验的是 JSONB 的合并语义和状态机流转。

两件事最要紧：

1. **原文不能被脱敏结果覆盖。** 覆盖了之后，"E1-T5 到底脱干净没有"这个核对
   就再也做不了了 —— 而 AC 要求抽 20 条人工核对，人核对的正是这两份的差。
2. **状态流转要和 §7.4 对得上**：`DISCOVERED` → `PRESCREENED` / `REJECTED`，
   不越级、不回退。
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.prescreen import parse_verdict
from app.domain.enums import TaskCandidateState
from app.infrastructure.models.benchmark import TaskCandidate
from cli.mine import upsert_candidates, upsert_repository
from cli.prescreen import _write_verdict, clean_candidate
from tests.integration.factories import wipe
from tests.integration.test_mining_persistence import REPO, REPO_META, make_pr

pytestmark = pytest.mark.db

FULL_HASH = "2d610e36a429bfebf0adb0ca90cdc0585f296369"

#: 一条**带泄题**的 issue 正文：PR 链接 + 40 位哈希 + 补丁块，三样都有。
DIRTY_BODY = (
    f"调用 convert 的时候没去掉空格。修复见 https://github.com/{REPO}/pull/2946，"
    f"提交 {FULL_HASH}。\n\n"
    "```python\nPath('x ').convert()\n```\n\n"
    "diff --git a/src/click/types.py b/src/click/types.py\n"
    "--- a/src/click/types.py\n+++ b/src/click/types.py\n"
    "@@ -1,1 +1,1 @@\n-        return value\n+        return value.strip()\n"
)

DIFF = """\
diff --git a/src/click/types.py b/src/click/types.py
--- a/src/click/types.py
+++ b/src/click/types.py
@@ -10,2 +10,2 @@ class Path:
-        return value
+        return value.strip()
diff --git a/tests/test_types.py b/tests/test_types.py
--- a/tests/test_types.py
+++ b/tests/test_types.py
@@ -1,1 +1,4 @@
 import click
+
+def test_path_strips():
+    assert click.Path().convert(" x ") == "x"
"""


@pytest.fixture
def candidate(session: Session) -> tuple[int, dict[str, Any]]:
    """库里放一条挖好的候选，正文是脏的（带泄题）。"""
    wipe(session)
    repo_id = upsert_repository(session, REPO, REPO_META, is_domestic=False).id
    pr = make_pr(2946)
    upsert_candidates(session, repo_id, [pr], repo=REPO)
    session.flush()

    row = session.execute(sa.select(TaskCandidate)).scalar_one()
    payload = dict(row.raw_payload)
    payload["issues"] = [{"number": 100, "title": "去不掉空格", "body": DIRTY_BODY, "url": "u"}]
    row.raw_payload = payload
    session.flush()
    return row.id, payload


def reload(session: Session, row_id: int) -> TaskCandidate:
    session.expire_all()
    return session.execute(sa.select(TaskCandidate).where(TaskCandidate.id == row_id)).scalar_one()


# ══════════════════════════════════════════════════════════════
# 清洗
# ══════════════════════════════════════════════════════════════


def test_cleaning_writes_a_redacted_copy_and_keeps_the_original(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """**原文一个字不改，脱敏结果写进新的 `cleaned` 段。**

    覆盖掉原文的话，"E1-T5 到底脱干净没有"这个核对就再也做不了了 ——
    而 AC 要求抽 20 条人工核对，人核对的正是这两份的差。
    """
    row_id, payload = candidate
    cleaned, leaks = clean_candidate(payload, repo=REPO, diff=DIFF)
    session.execute(
        sa.update(TaskCandidate)
        .where(TaskCandidate.id == row_id)
        .values(raw_payload={**payload, "cleaned": cleaned})
    )
    session.flush()

    row = reload(session, row_id)
    assert FULL_HASH in row.raw_payload["issues"][0]["body"], "原文必须原样留着"
    assert FULL_HASH not in row.raw_payload["cleaned"]["issue_body"], "脱敏那份要干净"
    assert leaks == []


def test_cleaning_records_what_it_removed(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """记账是为了让报表能回答"这批题面被动了多少"。

    一条 issue 被剥掉好几处，多半说明它本来就在讨论具体的修复过程，值得人看一眼。
    """
    _, payload = candidate
    cleaned, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    counts = cleaned["redaction"]["body"]
    assert counts["pr_links"] == 1
    assert counts["hashes"] == 1
    assert counts["diff_blocks"] == 1


def test_cleaning_keeps_the_reproduction_code(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """复现代码是好 issue 的特征，剥了会把题面剥废。"""
    _, payload = candidate
    cleaned, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    assert "Path('x ').convert()" in cleaned["issue_body"]


def test_cleaning_splits_the_patch_and_extracts_f2p_candidates(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    _, payload = candidate
    cleaned, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    assert cleaned["patches"]["test_paths"] == ["tests/test_types.py"]
    assert cleaned["patches"]["code_paths"] == ["src/click/types.py"]
    assert cleaned["patches"]["usable"] is True
    assert cleaned["f2p_candidates"] == ["tests/test_types.py::test_path_strips"]


def test_cleaning_twice_gives_the_same_result(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """clean 不花钱，所以要能随便重跑。除了时间戳，结果必须一模一样。"""
    _, payload = candidate
    first, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    second, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    for key in ("issue_body", "issue_title", "patches", "f2p_candidates", "redaction"):
        assert first[key] == second[key]


# ══════════════════════════════════════════════════════════════
# 打分之后的状态流转（§7.4）
# ══════════════════════════════════════════════════════════════


def verdict(score: float, **overrides: Any) -> Any:
    base = {
        "self_contained": True,
        "leaks_fix": False,
        "over_specified": False,
        "locatable": True,
        "score": score,
        "reason": "还行",
    }
    return parse_verdict({**base, **overrides}, flags=tuple(overrides.pop("flags", ())))


def test_a_passing_candidate_becomes_prescreened(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    row_id, payload = candidate
    _write_verdict(session, row_id, payload, verdict(5))
    session.flush()

    row = reload(session, row_id)
    assert row.state is TaskCandidateState.PRESCREENED
    assert float(row.prescreen_score) == 5.0
    assert row.prescreen_reason == "还行"
    assert row.reject_reason is None, "没被拒的行不该有拒绝理由"


def test_a_rejected_candidate_records_why(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    row_id, payload = candidate
    _write_verdict(session, row_id, payload, verdict(1, reason="正文几乎没信息"))
    session.flush()

    row = reload(session, row_id)
    assert row.state is TaskCandidateState.REJECTED
    assert row.reject_reason == "正文几乎没信息"


def test_a_leaking_candidate_is_rejected_however_high_the_score(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """泄题一票否决 —— 那道题会被所有 Agent 满分通过，而排行榜上看不出异常。"""
    row_id, payload = candidate
    _write_verdict(session, row_id, payload, verdict(5, leaks_fix=True))
    session.flush()
    assert reload(session, row_id).state is TaskCandidateState.REJECTED


def test_the_full_verdict_survives_in_the_payload(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """列是给 SQL 查询用的投影，完整结论留在 JSONB 里 ——
    将来加一个判断维度不用改表。
    """
    row_id, payload = candidate
    _write_verdict(session, row_id, payload, verdict(4, over_specified=True))
    session.flush()

    stored = reload(session, row_id).raw_payload["prescreen"]
    assert stored["over_specified"] is True
    assert stored["decision"] == "PASS"
    assert stored["prompt_version"]


def test_scoring_does_not_disturb_the_mined_or_cleaned_data(
    session: Session, candidate: tuple[int, dict[str, Any]]
) -> None:
    """打分只加一段，不动前两步的产出。"""
    row_id, payload = candidate
    cleaned, _ = clean_candidate(payload, repo=REPO, diff=DIFF)
    payload = {**payload, "cleaned": cleaned}
    _write_verdict(session, row_id, payload, verdict(5))
    session.flush()

    stored = reload(session, row_id).raw_payload
    assert stored["cleaned"]["f2p_candidates"] == cleaned["f2p_candidates"]
    assert stored["base_commit"] == payload["base_commit"], "E1-T4 挖的东西原样还在"
    assert FULL_HASH in stored["issues"][0]["body"]
