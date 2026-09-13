"""排行榜聚合的口径（E7-T0）。

测的是 `app.evaluation.leaderboard.summarize()` 这个纯函数：
多轮怎么合成一行、名次怎么排、成本来源怎么报、分面怎么合并。
不起数据库 —— "谁有资格上榜"那部分要查库，在
`tests/integration/test_api_leaderboard.py` 里测。
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import CostSource
from app.evaluation.leaderboard import (
    CostSourceCount,
    EligibleRun,
    FacetCount,
    LeaderboardMetric,
    summarize,
)

PROTOCOL = "v1.2"


def run(
    run_id: int,
    *,
    config_id: int = 1,
    label: str = "aider@deepseek-chat",
    rate: str = "0.0909",
    resolved: int = 2,
    tasks: int = 22,
    cost: str = "0.30",
    tokens: int = 1_000_000,
    makespan_ms: int | None = 600_000,
    protocol: str = PROTOCOL,
    infra_failures: int = 0,
    retries: int = 0,
) -> EligibleRun:
    """造一条合格实验。默认值照着库里 #125 那一轮的量级写。"""
    return EligibleRun(
        evaluation_run_id=run_id,
        agent_config_id=config_id,
        label=label,
        agent_name=label.split("@")[0],
        agent_display_name=label.split("@")[0].title(),
        agent_version="1.0",
        model_name=label.split("@")[-1],
        protocol_version=protocol,
        benchmark_set_id=123,
        total_tasks=tasks,
        resolved_count=resolved,
        strict_resolve_rate=Decimal(rate),
        effective_resolve_rate=Decimal(rate),
        infra_failure_count=infra_failures,
        retry_count=retries,
        total_cost_usd=Decimal(cost),
        total_tokens=tokens,
        makespan_ms=makespan_ms,
        finished_at=None,
    )


def test_rounds_of_one_contestant_collapse_into_one_row() -> None:
    """同一个参赛者的多轮合成一行，平均和离散度都在行里。

    为什么要离散度：实测同一批题跑两遍有五分之一的题会改结论
    （§18.6 第六节），只报一个平均数没法解释 MET-01 的"偏差 ≤5 个百分点"。
    """
    rows = summarize([run(125, rate="0.0909", resolved=2), run(126, rate="0.1818", resolved=4)])

    assert len(rows) == 1
    row = rows[0]
    assert row.run_count == 2
    assert row.run_ids == (125, 126)
    assert row.resolve_rate_mean == Decimal("0.1364")  # (0.0909 + 0.1818) / 2
    assert row.resolve_rate_min == Decimal("0.0909")
    assert row.resolve_rate_max == Decimal("0.1818")
    assert row.resolve_rate_spread == Decimal("0.0909")
    assert row.resolved_mean == Decimal("3.0000")


def test_same_contestant_under_two_protocol_versions_gets_two_rows() -> None:
    """协议版本不同就分两行（协议 C-59：不同门槛的结果不能混排）。"""
    rows = summarize(
        [
            run(1, protocol="v1.2", rate="0.5000"),
            run(2, protocol="v1.3", rate="0.9000"),
        ]
    )

    assert [(r.protocol_version, r.resolve_rate_mean) for r in rows] == [
        ("v1.3", Decimal("0.9000")),
        ("v1.2", Decimal("0.5000")),
    ]


def test_rank_is_by_mean_resolve_rate_descending() -> None:
    """默认按平均解决率从高到低，名次从 1 开始。"""
    rows = summarize(
        [
            run(125, config_id=1, label="aider@deepseek-chat", rate="0.0909"),
            run(127, config_id=2, label="claude-code@deepseek-chat", rate="0.8636"),
        ]
    )

    assert [(r.rank, r.label) for r in rows] == [
        (1, "claude-code@deepseek-chat"),
        (2, "aider@deepseek-chat"),
    ]


def test_ties_break_on_a_unique_key_so_paging_is_stable() -> None:
    """解决率和成本完全相同的两行，顺序必须是确定的（AC-4）。

    没有唯一兜底键的话，同一个 offset 翻两次可能给出两个顺序，
    前端翻页会重复或漏行 —— 而且后端日志里什么都看不出来。
    """
    tied = [
        run(1, config_id=7, label="b@m", rate="0.5000", cost="1.00"),
        run(2, config_id=3, label="a@m", rate="0.5000", cost="1.00"),
    ]
    first = [r.agent_config_id for r in summarize(tied)]
    second = [r.agent_config_id for r in summarize(list(reversed(tied)))]

    assert first == second == [3, 7]


def test_cost_metric_sorts_cheapest_first_and_puts_unknown_last() -> None:
    """按成本排时，报不出成本的（每题成本为 None）排最后，不是排最前。"""
    rows = summarize(
        [
            run(1, config_id=1, label="pricey@m", cost="10.00"),
            run(2, config_id=2, label="cheap@m", cost="1.00"),
            run(3, config_id=3, label="unknown@m", cost="0", tasks=0),
        ],
        metric=LeaderboardMetric.COST,
    )

    assert [r.label for r in rows] == ["cheap@m", "pricey@m", "unknown@m"]
    assert rows[-1].cost_per_task is None


def test_cost_sources_are_reported_separately() -> None:
    """三种成本来源分开报（协议纪律 3）。

    claude-code 走中转端点时 44 次全报 `unavailable`，它的
    `total_cost_usd` 就是 0 —— 读起来是"不花钱"，而钱一分没少花。
    前端要靠 `cost_unavailable_attempts` 知道那个金额是缺的，不是零。
    """
    rows = summarize(
        [run(127, cost="0", tokens=18_910_643), run(128, cost="0", tokens=20_465_154)],
        costs=[
            CostSourceCount(127, CostSource.UNAVAILABLE, 22),
            CostSourceCount(128, CostSource.UNAVAILABLE, 22),
        ],
    )

    row = rows[0]
    assert row.cost_unavailable_attempts == 44
    assert row.cost_reported_attempts == 0
    assert row.cost_usd_total == Decimal("0")


def test_cost_source_null_attempts_count_as_neither() -> None:
    """`cost_source` 为 NULL 的 attempt 不进任何一档。

    NULL 的含义是"还没跑到报成本那一步"（被取消、还在跑），
    不是"报不出成本"。混进 unavailable 会让前端以为有一次真实的缺口。
    """
    rows = summarize(
        [run(1)],
        costs=[CostSourceCount(1, None, 5), CostSourceCount(1, CostSource.REPORTED, 17)],
    )

    row = rows[0]
    assert row.cost_reported_attempts == 17
    assert row.cost_unavailable_attempts == 0
    assert row.cost_estimated_attempts == 0


def test_cost_per_task_is_unknown_when_any_attempt_could_not_report() -> None:
    """有 attempt 报不出成本时，每题成本是 None，不是一个偏小的数。

    这条挡的是一个会让排行榜颠倒的错：claude-code 走中转端点时 44 次全报
    `unavailable`，总额是 $0.00。按"已知部分 / 全部题数"算出来就是每题 $0，
    于是 `?metric=cost` 把它排在第一，理由是"免费" —— 而 §18.6 第七节
    手算出来它是 $0.042/题，比 aider 贵 2.4 倍。
    """
    rows = summarize(
        [run(127, cost="0", config_id=2, label="claude-code@m")],
        costs=[CostSourceCount(127, CostSource.UNAVAILABLE, 22)],
    )

    assert rows[0].cost_per_task is None
    # 金额本身照样给出来，配上计数让人自己判断有多少水分
    assert rows[0].cost_usd_total == Decimal("0")
    assert rows[0].cost_unavailable_attempts == 22


def test_contestant_with_unknown_cost_does_not_win_the_cost_ranking() -> None:
    """按成本排时，"报不出成本"不等于"最便宜"。"""
    rows = summarize(
        [
            run(1, config_id=1, label="reports@m", cost="0.44"),
            run(2, config_id=2, label="silent@m", cost="0"),
        ],
        costs=[
            CostSourceCount(1, CostSource.REPORTED, 22),
            CostSourceCount(2, CostSource.UNAVAILABLE, 22),
        ],
        metric=LeaderboardMetric.COST,
    )

    assert [r.label for r in rows] == ["reports@m", "silent@m"]


def test_cost_per_task_divides_by_all_rounds() -> None:
    """每题成本的分母是"轮数 × 每轮题数"，不是一轮的题数。"""
    rows = summarize(
        [run(1, cost="0.44", tasks=22), run(2, cost="0.44", tasks=22)],
        costs=[
            CostSourceCount(1, CostSource.REPORTED, 22),
            CostSourceCount(2, CostSource.REPORTED, 22),
        ],
    )

    assert rows[0].cost_usd_total == Decimal("0.88")
    assert rows[0].cost_per_task == Decimal("0.020000")  # 0.88 / 44


def test_facets_merge_across_rounds() -> None:
    """分面跨轮相加，不是每轮各算一个比例再平均。

    22 道题分到三个难度，一格可能只有 3 道 —— 一轮一轮看噪声太大，
    合起来分母才够用。
    """
    rows = summarize(
        [run(1), run(2)],
        facets=[
            FacetCount(1, "easy", resolved=2, total=6),
            FacetCount(1, "hard", resolved=0, total=4),
            FacetCount(2, "easy", resolved=4, total=6),
            FacetCount(2, "hard", resolved=1, total=4),
        ],
    )

    cells = {cell.value: cell for cell in rows[0].facets}
    assert cells["easy"].resolved == 6
    assert cells["easy"].total == 12
    assert cells["easy"].resolve_rate == Decimal("0.5000")
    assert cells["hard"].resolve_rate == Decimal("0.1250")
    # 按取值排序，前端不用自己排
    assert [cell.value for cell in rows[0].facets] == ["easy", "hard"]


def test_makespan_mean_ignores_rounds_that_never_recorded_one() -> None:
    """没记 makespan 的轮不拉低平均；一轮都没记就是 None，不是 0。"""
    assert (
        summarize([run(1, makespan_ms=600_000), run(2, makespan_ms=None)])[0].makespan_ms_mean
        == 600_000
    )
    assert summarize([run(1, makespan_ms=None)])[0].makespan_ms_mean is None


def test_infra_failures_and_retries_are_summed_not_hidden() -> None:
    """平台故障数和重试数要报出来（协议 C-56）。

    能上榜说明每一轮都没超 C-26 的 5%，但"零故障跑完"和"每轮都擦着门槛过"
    可信度不一样，榜单不该把这件事藏起来。
    """
    row = summarize([run(1, infra_failures=1, retries=3), run(2, infra_failures=0, retries=1)])[0]

    assert row.infra_failure_total == 1
    assert row.retry_total == 4


def test_empty_input_gives_empty_leaderboard() -> None:
    """一个合格实验都没有时返回空表，不抛异常。"""
    assert summarize([]) == []
