"""有效并发时间序列的扫描线（E5-T2，服务 MET-03）。

ADR-012 把"并行度"定义成"同时处于已开始但还没结束状态的评测任务数"，
`sweep()` 就是那句定义的算法。这组测试盯住三件容易算错的事：

1. **端点相接不能出现假的凹口** —— 一道题在另一道题结束的同一时刻开始，
   并发数应该平着走，不是先掉到 0 再弹回 1；
2. **P50 按时间加权** —— 按变化点数取中位数会被"密集变化的那一秒"带偏；
3. **三条曲线各算各的** —— 双层并发的证据正是"in_flight 顶在 8、sandbox 压在 5"。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.evaluation.concurrency import Interval, summarize, sweep, to_csv

T0 = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def span(start_s: float, end_s: float) -> Interval:
    return Interval(start=T0 + timedelta(seconds=start_s), end=T0 + timedelta(seconds=end_s))


def values(points: list, curve: str = "in_flight") -> list[int]:
    return [p.value(curve) for p in points]


def test_disjoint_intervals_never_exceed_one() -> None:
    points = sweep([span(0, 10), span(20, 30)])
    assert values(points) == [1, 0, 1, 0]
    assert summarize(points).peak == 1


def test_overlapping_intervals_stack_up() -> None:
    points = sweep([span(0, 30), span(10, 40), span(20, 25)])
    assert summarize(points).peak == 3
    assert values(points) == [1, 2, 3, 2, 1, 0]


def test_touching_endpoints_do_not_create_a_fake_dip() -> None:
    """一道题 10 秒结束、另一道 10 秒开始 → 并发数一直是 1。

    区间是左闭右开的。同一时刻的加减一起结算再出点，不然图上会出现一个
    宽度为零的凹口，看图的人会以为那一瞬间机器空了。
    """
    points = sweep([span(0, 10), span(10, 20)])
    assert values(points) == [1, 1, 0]
    assert min(values(points)[:-1]) == 1


def test_zero_length_intervals_contribute_nothing() -> None:
    """瞬间结束的执行（比如取消得早）不该在图上留下一个 +1。"""
    assert values(sweep([span(5, 5)])) == [0]


def test_backwards_intervals_are_dropped() -> None:
    """结束时刻早于开始时刻的行是脏数据，直接跳过，不能让它把计数带成负数。"""
    assert sweep([Interval(start=T0 + timedelta(seconds=10), end=T0)]) == []


def test_three_curves_are_counted_separately() -> None:
    """双层并发的证据：在途 3 条，AI 层 2 条，测试层被压到 1 条。"""
    points = sweep(
        in_flight=[span(0, 30), span(0, 30), span(0, 30)],
        agent=[span(1, 10), span(2, 10)],
        sandbox=[span(11, 20)],
    )
    assert summarize(points, "in_flight").peak == 3
    assert summarize(points, "agent").peak == 2
    assert summarize(points, "sandbox").peak == 1


def test_p50_is_weighted_by_time_not_by_the_number_of_change_points() -> None:
    """一条 100 秒的低并发 + 一次 1 秒的尖峰 → P50 是低的那个。

    按变化点取中位数的话，那 1 秒里挤着的两个点会和 100 秒等权，
    报出来的 P50 会虚高。MET-03 问的是"有一半时间里并发不低于多少"。
    """
    points = sweep([span(0, 100), span(50, 51)])
    result = summarize(points)
    assert result.peak == 2
    assert result.p50 == 1
    assert result.span_s == 100.0


def test_p50_reaches_the_target_when_the_run_is_actually_parallel() -> None:
    """8 条题几乎同时跑满全程 → P50 = 8，这就是 MET-03 要的那个数。"""
    points = sweep([span(0, 600) for _ in range(8)])
    result = summarize(points)
    assert (result.peak, result.p50) == (8, 8)


def test_empty_series_is_all_zeros() -> None:
    result = summarize([])
    assert (result.peak, result.p50, result.span_s) == (0, 0, 0.0)


def test_csv_has_a_header_and_one_row_per_change_point() -> None:
    lines = to_csv(sweep([span(0, 10)])).strip().splitlines()
    assert lines[0] == "timestamp,in_flight,agent,sandbox"
    assert len(lines) == 3  # 表头 + 起点 + 终点
    assert lines[1].endswith(",1,0,0")
    assert lines[2].endswith(",0,0,0")
