"""数据集快照与发布门禁的纯函数（E1-T6）。

这一组不碰数据库。门禁的判断规则（`check_sentinel`）和快照的身份算法
（`snapshot_digest`）都要能脱开一整套评测链路单独测 —— 它们出错的表现是
"发布了一个不该发布的数据集"，而那种错在集成测试里要跑几十个容器才看得见。
"""

from __future__ import annotations

import pytest

from app.benchmark.dataset import (
    Drift,
    SentinelResult,
    SnapshotRow,
    build_manifest,
    check_sentinel,
    next_version,
    plan_stage,
    sha256_of,
    snapshot_digest,
)
from app.domain.enums import BenchmarkSetStatus, EvaluationRunStatus
from app.infrastructure.models.benchmark import BenchmarkSet


def row(task_id: str, content_hash: str = "a") -> SnapshotRow:
    """一行快照。`content_hash` 只给首字母，这里在意的是"变没变"不是它的值。"""
    return SnapshotRow(
        benchmark_task_id=abs(hash(task_id)) % 10_000,
        task_id=task_id,
        content_hash=content_hash * 64,
    )


# ── 快照摘要 ────────────────────────────────────────────────


def test_digest_ignores_row_order() -> None:
    """同样一批题，先入库谁后入库谁不该算成两个不同的数据集。"""
    forward = [row("a__x-1"), row("a__x-2"), row("a__x-3")]
    assert snapshot_digest(forward) == snapshot_digest(list(reversed(forward)))


def test_digest_changes_when_content_changes() -> None:
    """题还是那道题，内容改了，摘要必须变 —— 否则"同一个版本"是假的。"""
    before = [row("a__x-1", "a"), row("a__x-2", "b")]
    after = [row("a__x-1", "a"), row("a__x-2", "c")]
    assert snapshot_digest(before) != snapshot_digest(after)


def test_digest_changes_when_membership_changes() -> None:
    """少一道题，摘要也必须变。"""
    full = [row("a__x-1"), row("a__x-2")]
    assert snapshot_digest(full) != snapshot_digest(full[:1])


def test_digest_is_prefixed_sha256() -> None:
    digest = snapshot_digest([row("a__x-1")])
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_empty_snapshot_has_a_digest() -> None:
    """空快照也算得出摘要。它不该抛异常 —— 拒绝空快照是 `stage` 的事，不是哈希的事。"""
    assert snapshot_digest([]).startswith("sha256:")


# ── 版本号 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ([], "v1"),
        (["v1"], "v2"),
        (["v1", "v2"], "v3"),
        # 中间空了一号也接着最大的往下走，不去填坑
        (["v1", "v3"], "v4"),
        # 看不懂的写法（比如手工填的 1.0）不参与计算，也不报错
        (["1.0"], "v1"),
        (["v2", "1.0"], "v3"),
        (["v10", "v9"], "v11"),
    ],
)
def test_next_version(existing: list[str], expected: str) -> None:
    assert next_version(existing) == expected


# ── 门禁的四条检查 ──────────────────────────────────────────


def sentinel(
    *,
    agent: str = "oracle",
    status: EvaluationRunStatus = EvaluationRunStatus.COMPLETED,
    total: int = 3,
    resolved: tuple[str, ...] = ("t1", "t2", "t3"),
    not_clean: tuple[str, ...] = (),
) -> SentinelResult:
    everyone = ("t1", "t2", "t3")
    return SentinelResult(
        run_id=1,
        agent_name=agent,
        status=status,
        total_tasks=total,
        completed_tasks=len(everyone),
        resolved_count=len(resolved),
        not_clean=not_clean,
        resolved_tasks=resolved,
        unresolved_tasks=tuple(t for t in everyone if t not in resolved),
    )


def test_oracle_all_resolved_passes() -> None:
    assert check_sentinel(sentinel(), agent_name="oracle", expected_tasks=3, want_resolved=3) == []


def test_noop_none_resolved_passes() -> None:
    result = sentinel(agent="noop", resolved=())
    assert check_sentinel(result, agent_name="noop", expected_tasks=3, want_resolved=0) == []


def test_missing_sentinel_is_rejected() -> None:
    """门禁根本没跑 ≠ 门禁通过。"""
    problems = check_sentinel(None, agent_name="oracle", expected_tasks=3, want_resolved=3)
    assert len(problems) == 1
    assert "还没跑" in problems[0]


def test_unfinished_run_is_rejected() -> None:
    result = sentinel(status=EvaluationRunStatus.RUNNING)
    problems = check_sentinel(result, agent_name="oracle", expected_tasks=3, want_resolved=3)
    assert any("RUNNING" in p for p in problems)


def test_task_count_mismatch_is_rejected() -> None:
    """门禁跑的题数和快照对不上，说明跑的不是这一批。"""
    result = sentinel(total=2)
    problems = check_sentinel(result, agent_name="oracle", expected_tasks=3, want_resolved=3)
    assert any("快照里有 3 道" in p for p in problems)


def test_oracle_below_100_names_the_offenders() -> None:
    """Oracle 掉出 100%，报错必须点名是哪几道题 —— 只报个百分比没法查。"""
    result = sentinel(resolved=("t1", "t2"))
    problems = check_sentinel(result, agent_name="oracle", expected_tasks=3, want_resolved=3)
    assert any("t3" in p and "66.7%" in p for p in problems)


def test_noop_above_zero_names_the_offenders() -> None:
    """Noop 解出来了 —— 那道题在修复前测试就通过，是坏题。"""
    result = sentinel(agent="noop", resolved=("t2",))
    problems = check_sentinel(result, agent_name="noop", expected_tasks=3, want_resolved=0)
    assert any("t2" in p for p in problems)


def test_infra_failure_blocks_even_when_the_rate_looks_right() -> None:
    """**这条是门禁的第三条检查存在的理由。**

    一道题因为平台故障没跑成，它同样不是 RESOLVED，于是 Noop 那边的"0%"可以被
    凑出来 —— 只查解决率的话门禁会放行，而那道题根本没验过。
    """
    result = sentinel(agent="noop", resolved=(), not_clean=("t2",))
    problems = check_sentinel(result, agent_name="noop", expected_tasks=3, want_resolved=0)
    assert len(problems) == 1
    assert "没拿到干净的结果" in problems[0]
    assert "t2" in problems[0]


def test_problem_list_truncates_long_offender_lists() -> None:
    """22 道题全挂的时候，报错不该刷满一屏。"""
    many = tuple(f"t{i}" for i in range(1, 21))
    result = SentinelResult(
        run_id=7,
        agent_name="oracle",
        status=EvaluationRunStatus.COMPLETED,
        total_tasks=20,
        completed_tasks=20,
        resolved_count=0,
        not_clean=(),
        resolved_tasks=(),
        unresolved_tasks=many,
    )
    problems = check_sentinel(result, agent_name="oracle", expected_tasks=20, want_resolved=20)
    assert any("等 20 道" in p for p in problems)


# ── 指纹 ────────────────────────────────────────────────────


def test_manifest_has_the_fields_32_6_asks_for() -> None:
    """字段清单照 `12-engineering-workflow.md` §32.6。"""
    dataset = BenchmarkSet(
        slug="benchmark-dev",
        version="v1",
        title="t",
        status=BenchmarkSetStatus.PUBLISHED,
        source_dataset_id="benchmark-dev",
        published_at=None,
    )
    manifest = build_manifest(
        dataset,
        [row("a__x-2"), row("a__x-1")],
        dataset_sha256="sha256:" + "0" * 64,
        harness_git_sha="deadbeef",
        dirty=False,
        evidence={"oracle": {"resolve_rate": 1.0}},
    )
    for key in (
        "slug",
        "version",
        "task_count",
        "dataset_sha256",
        "task_hashes_sha256",
        "published_at",
        "harness_git_sha",
    ):
        assert key in manifest, key
    assert manifest["task_count"] == 2
    # 题目清单排序，两次导出才逐字节相同
    assert [t["task_id"] for t in manifest["tasks"]] == ["a__x-1", "a__x-2"]  # type: ignore[index,union-attr]


def test_manifest_records_dirty_honestly() -> None:
    """协议 C-28：脏工作区跑出来的结果要标记出来，不许留一个恒为 false 的字段。"""
    dataset = BenchmarkSet(slug="s", version="v1", title="t", status=BenchmarkSetStatus.PUBLISHED)
    manifest = build_manifest(
        dataset,
        [row("a__x-1")],
        dataset_sha256="sha256:x",
        harness_git_sha="abc",
        dirty=True,
        evidence={},
    )
    assert manifest["dirty"] is True


def test_sha256_of_is_stable() -> None:
    assert sha256_of("abc") == sha256_of("abc")
    assert sha256_of("abc") != sha256_of("abd")
    assert sha256_of("").startswith("sha256:")


# ── 漂移 ────────────────────────────────────────────────────


def test_drift_clean_only_when_all_three_are_empty() -> None:
    assert Drift(changed=(), quarantined=(), missing=()).clean
    assert not Drift(changed=("a",), quarantined=(), missing=()).clean
    assert not Drift(changed=(), quarantined=("a",), missing=()).clean
    assert not Drift(changed=(), quarantined=(), missing=("a",)).clean


# ── 这次 stage 该干什么 ─────────────────────────────────────


def a_set(version: str, status: BenchmarkSetStatus, digest: str = "a") -> BenchmarkSet:
    return BenchmarkSet(
        slug="s", version=version, title="t", status=status, snapshot_digest=digest * 64
    )


def test_stage_creates_v1_when_there_is_nothing() -> None:
    plan = plan_stage([], "a" * 64)
    assert (plan.action, plan.version) == ("create", "v1")


def test_stage_refreshes_the_latest_draft() -> None:
    """草稿本来就是拿来改的。"""
    existing = [a_set("v2", BenchmarkSetStatus.DRAFT), a_set("v1", BenchmarkSetStatus.PUBLISHED)]
    plan = plan_stage(existing, "b" * 64)
    assert (plan.action, plan.version) == ("refresh", "v2")


def test_stage_is_a_noop_when_the_published_version_already_matches() -> None:
    """**同一条命令跑两遍不该让版本号涨。**

    E8-T2 在 `assemble --limit` 上栽过同一类跟头：跑第二遍集合悄悄变大，
    比产生重复题更难发现 —— 重复题一眼看得出来，多出来的题看着跟正常入库的一样。
    """
    existing = [a_set("v1", BenchmarkSetStatus.PUBLISHED, "a")]
    plan = plan_stage(existing, "a" * 64)
    assert (plan.action, plan.same_as) == ("noop", "v1")


def test_stage_creates_a_new_version_when_the_content_changed() -> None:
    existing = [a_set("v1", BenchmarkSetStatus.PUBLISHED, "a")]
    plan = plan_stage(existing, "b" * 64)
    assert (plan.action, plan.version) == ("create", "v2")


def test_stage_refreshes_a_draft_even_if_it_matches_a_published_version() -> None:
    """草稿的内容碰巧和已发布版一样时，该刷的还是刷，只是提醒一句。

    拒绝动它的话，那份草稿会永远停在旧内容上。
    """
    existing = [
        a_set("v2", BenchmarkSetStatus.DRAFT, "b"),
        a_set("v1", BenchmarkSetStatus.PUBLISHED, "a"),
    ]
    plan = plan_stage(existing, "a" * 64)
    assert plan.action == "refresh"
    assert plan.same_as == "v1"


def test_stage_ignores_archived_versions_when_matching() -> None:
    """归档的版本不算"已经发布过这批题"—— 它已经退役了。"""
    existing = [a_set("v1", BenchmarkSetStatus.ARCHIVED, "a")]
    plan = plan_stage(existing, "a" * 64)
    assert (plan.action, plan.version) == ("create", "v2")
