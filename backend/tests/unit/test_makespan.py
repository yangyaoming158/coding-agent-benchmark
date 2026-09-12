"""Makespan 投影模型（E9-T1）。

两类测试，目的不一样：

**① 复现 §18.2 那张表。** 文档里的输入能不能算出文档里的输出。改公式会红 ——
那时候该改的是公式还是文档，得停下来想一想，不能直接把断言改掉。

**② 钉住"`P_agent` 会被槽位封顶"这件事**
（`test_shipped_defaults_cap_agent_limit_at_worker_slots`）。这是 E9-T1 查出来的，
§18.2 成文时不知道。它最容易被谁顺手"简化"掉 —— 毕竟
`min(agent_limit, worker_slots)` 看起来像多余的防御。红了说明有人把它删了。
"""

from __future__ import annotations

import pytest

from app.domain.makespan import (
    DEFAULT_OVERHEAD_RATIO,
    DEGRADATION_BANDS,
    TARGET_HOURS,
    TARGET_RUNS,
    MakespanInputs,
    max_agent_minutes,
    measured_overhead,
    project,
)
from app.infrastructure.config import Settings

#: §18.2 那张表的共同假设：300 次运行、A=6 分钟、S=1.7 分钟。
DOC_RUNS = 300
DOC_AGENT_MINUTES = 6.0
DOC_OTHER_MINUTES = 1.7


def _doc_row(
    *,
    agent_limit: int,
    sandbox_limit: int,
    agent_minutes: float = DOC_AGENT_MINUTES,
    other_minutes: float = DOC_OTHER_MINUTES,
) -> MakespanInputs:
    """复现 §18.2 一行的输入。

    `worker_slots=None`（不封顶）是有意的：那张表直接给 `P_agent`，成文时还没发现
    槽位会封顶。填真实槽位数复现不出原表的数字 —— 而那个差异本身就是 E9-T1 的发现，
    由 `test_shipped_defaults_cap_agent_limit_at_worker_slots` 单独钉。
    """
    return MakespanInputs(
        runs=DOC_RUNS,
        agent_minutes=agent_minutes,
        other_minutes=other_minutes,
        agent_limit=agent_limit,
        sandbox_limit=sandbox_limit,
        worker_slots=None,
        overhead_ratio=DEFAULT_OVERHEAD_RATIO,
    )


# ── ① 复现 §18.2 那张表 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("agent_limit", "sandbox_limit", "agent_side", "sandbox_side", "hours", "with_overhead"),
    [
        # 表里的原话："本机当前 8C/10G：P_agent=8, P_sandbox=4"
        (8, 4, 225.0, 127.5, 3.8, 4.7),
        # "本机调 .wslconfig 后 16C/11G：P_agent=10, P_sandbox=5（已实施）"
        (10, 5, 180.0, 102.0, 3.0, 3.8),
        # "本机保守：P_agent=6, P_sandbox=3"
        (6, 3, 300.0, 170.0, 5.0, 6.3),
        # "云主机兜底 16C/32G：P_agent=12, P_sandbox=8"
        (12, 8, 150.0, 63.75, 2.5, 3.1),
    ],
)
def test_reproduces_the_documented_table(
    agent_limit: int,
    sandbox_limit: int,
    agent_side: float,
    sandbox_side: float,
    hours: float,
    with_overhead: float,
) -> None:
    """§18.2 四行标准配置，两侧时长和两个 makespan 都对得上。"""
    projection = project(_doc_row(agent_limit=agent_limit, sandbox_limit=sandbox_limit))

    assert projection.agent_side_minutes == pytest.approx(agent_side)
    assert projection.sandbox_side_minutes == pytest.approx(sandbox_side)
    # 文档里的小时数是按"≈"写的，所以比到 0.05 小时（3 分钟）
    assert projection.theoretical_minutes / 60 == pytest.approx(hours, abs=0.05)
    assert projection.projected_hours == pytest.approx(with_overhead, abs=0.05)


def test_documented_pessimistic_row_fails_the_target() -> None:
    """表里最后一行："Agent 均值 10 min（悲观）→ 375 分钟 → 6.3h（+25% = 7.8h）✗"。"""
    projection = project(_doc_row(agent_limit=8, sandbox_limit=4, agent_minutes=10.0))

    assert projection.agent_side_minutes == pytest.approx(375.0)
    assert projection.projected_hours == pytest.approx(7.8, abs=0.05)
    assert not projection.fits()


def test_documented_row_without_prebuilt_images() -> None:
    """表里的"无预建镜像（+2 min/题装依赖）"那行：`300 × 3.7 / 4 = 278 分钟`。

    只钉 Sandbox 侧。那一行的"≈4.6h + Agent 225 min 叠加"是文档里的散文 ——
    装依赖之后阶段不再干净地重叠，这个模型的 `max` 不表达那件事，不假装它表达。
    """
    projection = project(
        _doc_row(agent_limit=8, sandbox_limit=4, other_minutes=DOC_OTHER_MINUTES + 2.0)
    )

    assert projection.sandbox_side_minutes == pytest.approx(277.5)
    assert projection.sandbox_side_minutes / 60 == pytest.approx(4.6, abs=0.05)


def test_bottleneck_is_the_agent_side_in_every_documented_row() -> None:
    """§18.2 的结论 2："Agent 侧远大于 Sandbox 侧，瓶颈不在内存受限的那一层"。"""
    for agent_limit, sandbox_limit in [(8, 4), (10, 5), (6, 3), (12, 8)]:
        projection = project(_doc_row(agent_limit=agent_limit, sandbox_limit=sandbox_limit))
        assert projection.bottleneck == "agent", f"P_agent={agent_limit} 那行的瓶颈变了"


# ── ② P_agent 被槽位封顶 ────────────────────────────────────


def test_shipped_defaults_cap_agent_limit_at_worker_slots() -> None:
    """仓库默认值算出来的有效 `P_agent` 是 8，不是 `AGENT_CONCURRENCY=10`。

    `_env_file=None` 是必须的：开发机根目录有 `.env`，不屏蔽的话这条测试断言的是
    "这台机器现在配了什么"，而不是"仓库发出去的默认值是什么"。
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_concurrency == 10
    assert settings.worker_slots == 8

    inputs = MakespanInputs(
        runs=DOC_RUNS,
        agent_minutes=DOC_AGENT_MINUTES,
        other_minutes=DOC_OTHER_MINUTES,
        agent_limit=settings.agent_concurrency,
        sandbox_limit=settings.sandbox_concurrency,
        worker_slots=settings.worker_slots,
    )

    assert inputs.effective_agent_limit == 8, "槽位封顶没了 —— min(agent_limit, slots) 被删了？"
    # 于是 §18.2 表里"已实施"那行的 180 分钟在仓库默认值下拿不到，实际是 225
    assert project(inputs).agent_side_minutes == pytest.approx(225.0)


def test_raising_worker_slots_is_what_unlocks_p_agent_10() -> None:
    """降级表第三档的动作：槽位 8 → 10，Agent 侧从 225 分钟降到 180 分钟。"""
    base = {
        "runs": DOC_RUNS,
        "agent_minutes": DOC_AGENT_MINUTES,
        "other_minutes": DOC_OTHER_MINUTES,
        "agent_limit": 10,
        "sandbox_limit": 4,
    }
    before = project(MakespanInputs(**base, worker_slots=8))  # type: ignore[arg-type]
    after = project(MakespanInputs(**base, worker_slots=10))  # type: ignore[arg-type]

    assert before.agent_side_minutes == pytest.approx(225.0)
    assert after.agent_side_minutes == pytest.approx(180.0)
    # 省两成，和降级表里写的"Agent 侧省两成"对得上
    saved = 1 - after.agent_side_minutes / before.agent_side_minutes
    assert saved == pytest.approx(0.20, abs=0.01)


def test_sandbox_limit_is_capped_by_slots_too() -> None:
    """槽位比沙箱并发还小的时候，沙箱那一侧也要跟着降。"""
    inputs = MakespanInputs(
        runs=100,
        agent_minutes=1.0,
        other_minutes=1.0,
        agent_limit=10,
        sandbox_limit=8,
        worker_slots=3,
    )
    assert inputs.effective_sandbox_limit == 3


# ── 降级表 ──────────────────────────────────────────────────


def test_degradation_bands_are_ordered_and_closed() -> None:
    """档位必须从小到大排，最后一档必须没有上界，否则会查不到。"""
    bounds = [band.max_hours for band in DEGRADATION_BANDS]
    assert bounds[-1] is None, "最后一档必须是 max_hours=None"
    finite = [b for b in bounds[:-1] if b is not None]
    assert len(finite) == len(bounds) - 1, "只有最后一档允许是 None"
    assert finite == sorted(finite), "档位边界没有从小到大排"


@pytest.mark.parametrize(
    ("agent_minutes", "expected_band"),
    [
        (4.0, "ok"),  # 150 min → 3.1h
        (6.0, "ok"),  # 225 min → 4.7h，正好压在第一档的上界
        (7.0, "tight"),  # 262 min → 5.5h
        (7.68, "tight"),  # 360.0 min → 正好 6.00h，压在 MET-02 的线上
        (7.70, "raise_slots"),  # 360.9 min → 6.02h，差 1 分钟就过线了
        (9.0, "raise_slots"),  # 337 min → 7.0h
        (12.0, "change_terms"),  # 450 min → 9.4h，等于每道题都撞上硬超时
    ],
)
def test_band_lookup_at_shipped_concurrency(agent_minutes: float, expected_band: str) -> None:
    """按 `N=300、P_agent=8、损耗 25%` 查表。这组边界是 pilot 开跑之前定的。"""
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=agent_minutes,
        other_minutes=DOC_OTHER_MINUTES,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
        overhead_ratio=DEFAULT_OVERHEAD_RATIO,
    )
    assert project(inputs).band().key == expected_band


def test_agent_timeout_is_the_ceiling_on_a() -> None:
    """`agent_timeout_s=720` 给 `A` 封了顶 12 分钟，而 12 分钟落在最后一档。

    意思是：每道题都撞上硬超时的那个极端，模型算出来是 9.4 小时。
    所以超时率是 pilot 必须单列的指标 —— 它是 `A` 最大的单一来源。
    """
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=720 / 60,
        other_minutes=DOC_OTHER_MINUTES,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
    )
    projection = project(inputs)
    assert projection.projected_hours == pytest.approx(9.4, abs=0.05)
    assert projection.band().key == "change_terms"


# ── 反解：A 还能涨到多少 ────────────────────────────────────


def test_max_agent_minutes_is_the_inverse_of_the_projection() -> None:
    """反解出来的 `A` 代回去，投影应该正好落在验收线上。"""
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=5.0,
        other_minutes=DOC_OTHER_MINUTES,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
        overhead_ratio=DEFAULT_OVERHEAD_RATIO,
    )
    ceiling = max_agent_minutes(inputs)

    # P_agent=8、损耗 25%、300 次 → A ≤ 7.68 分钟
    assert ceiling == pytest.approx(7.68, abs=0.01)

    at_ceiling = project(
        MakespanInputs(
            runs=inputs.runs,
            agent_minutes=ceiling,
            other_minutes=inputs.other_minutes,
            agent_limit=inputs.agent_limit,
            sandbox_limit=inputs.sandbox_limit,
            worker_slots=inputs.worker_slots,
            overhead_ratio=inputs.overhead_ratio,
        )
    )
    assert at_ceiling.projected_hours == pytest.approx(TARGET_HOURS, abs=0.001)
    assert at_ceiling.fits()


def test_max_agent_minutes_is_zero_when_sandbox_side_alone_busts_the_budget() -> None:
    """光跑测试就超线的时候，`A` 再小也没用 —— 返回 0，别给一个假的余量。"""
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=1.0,
        other_minutes=30.0,  # 半小时跑一次测试
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
    )
    assert max_agent_minutes(inputs) == 0.0


# ── 损耗系数反算 ────────────────────────────────────────────


def test_measured_overhead_backs_out_the_ratio() -> None:
    assert measured_overhead(actual_minutes=125.0, theoretical_minutes=100.0) == pytest.approx(0.25)
    assert measured_overhead(actual_minutes=100.0, theoretical_minutes=100.0) == pytest.approx(0.0)


def test_measured_overhead_never_goes_negative() -> None:
    """实测比理论下限还快时算 0，不算负数。

    负的损耗没有物理含义，而且会让投影**偏乐观** —— 宁可把模型留在保守那一侧。
    实测快于理论下限是可能的：理论下限用的是均值，而真实负载里长任务和短任务
    会互相填空。
    """
    assert measured_overhead(actual_minutes=80.0, theoretical_minutes=100.0) == 0.0


def test_measured_overhead_with_no_work_is_zero() -> None:
    """理论下限是 0 的时候不除以零。"""
    assert measured_overhead(actual_minutes=10.0, theoretical_minutes=0.0) == 0.0


# ── 输入校验 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runs", 0),
        ("agent_minutes", -1.0),
        ("other_minutes", -0.1),
        ("agent_limit", 0),
        ("sandbox_limit", 0),
        ("worker_slots", 0),
        ("overhead_ratio", -0.1),
    ],
)
def test_rejects_nonsense_inputs(field: str, value: float) -> None:
    base: dict[str, float | int | None] = {
        "runs": 300,
        "agent_minutes": 6.0,
        "other_minutes": 1.7,
        "agent_limit": 10,
        "sandbox_limit": 4,
        "worker_slots": 8,
    }
    base[field] = value
    with pytest.raises(ValueError, match=field):
        MakespanInputs(**base)  # type: ignore[arg-type]


# ── 第三项：一道题拆不开并行 ────────────────────────────────


def test_single_task_floor_is_a_lower_bound() -> None:
    """最慢那道题的耗时本身就是 makespan 的下限。

    探测跑实测出来的那件事：4 次运行、8 个槽位，其中一次单独跑了 8.25 分钟。
    前两项摊平算只有 2.1 分钟，而真实 makespan 不可能小于那 8.25 分钟 ——
    一道题不能拆开让两个槽位一起跑。
    """
    inputs = MakespanInputs(
        runs=4,
        agent_minutes=4.2,
        other_minutes=0.08,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
        overhead_ratio=0.0,
        longest_task_minutes=8.25,
    )
    projection = project(inputs)

    assert projection.agent_side_minutes == pytest.approx(2.1)
    assert projection.theoretical_minutes == pytest.approx(8.25)
    assert projection.bottleneck == "single_task"


def test_single_task_floor_never_binds_at_300_runs() -> None:
    """投影 300 次时这一项永远不是瓶颈，所以 §18.2 的结论不受影响。

    最慢一道题顶多是 `agent_timeout_s=720` 的 12 分钟，而 Agent 侧是一百多分钟。
    """
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=4.2,
        other_minutes=0.08,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
        longest_task_minutes=12.0,
    )
    projection = project(inputs)

    assert projection.bottleneck == "agent"
    assert projection.theoretical_minutes == pytest.approx(projection.agent_side_minutes)


def test_documented_table_is_unaffected_by_the_new_term() -> None:
    """不给 `longest_task_minutes` 时这一项是 0，§18.2 原表照旧能复现。"""
    projection = project(_doc_row(agent_limit=8, sandbox_limit=4))
    assert projection.single_task_floor_minutes == 0.0
    assert projection.theoretical_minutes == pytest.approx(225.0)


def test_overhead_from_a_small_batch_is_nonsense_without_the_floor() -> None:
    """这条钉的是探测跑那个 296%：不算第三项，反算出来的损耗系数是垃圾。

    两个数摆在一起才说明问题 —— 所以这条测试同时算两遍。
    """
    common = {
        "runs": 4,
        "agent_minutes": 4.2,
        "other_minutes": 0.08,
        "agent_limit": 10,
        "sandbox_limit": 4,
        "worker_slots": 8,
        "overhead_ratio": 0.0,
    }
    without = project(MakespanInputs(**common))  # type: ignore[arg-type]
    with_floor = project(MakespanInputs(**common, longest_task_minutes=8.25))  # type: ignore[arg-type]

    bogus = measured_overhead(actual_minutes=8.3, theoretical_minutes=without.theoretical_minutes)
    sane = measured_overhead(actual_minutes=8.3, theoretical_minutes=with_floor.theoretical_minutes)

    assert bogus > 2.5, "不算第三项应该算出一个离谱的大数（实测 296%）"
    assert sane < 0.05, "算上第三项应该接近 0 —— 那批活根本没排过队"


# ── 批次够不够大 ────────────────────────────────────────────


def test_small_batch_does_not_saturate_slots() -> None:
    """探测跑那 4 次填不满 8 个槽位，所以它反算不出调度损耗。"""
    inputs = MakespanInputs(
        runs=4,
        agent_minutes=4.2,
        other_minutes=0.08,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
    )
    assert not inputs.saturates_slots


def test_full_pilot_batch_saturates_slots() -> None:
    """22 题 × 2 Agent × 3 轮 = 132 次，远超"两波"的判据，反算有效。"""
    inputs = MakespanInputs(
        runs=132,
        agent_minutes=4.2,
        other_minutes=0.08,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
    )
    assert inputs.saturates_slots


def test_saturation_threshold_is_two_waves() -> None:
    """边界：`N = 2 × P_agent` 算填满，再少一次就不算。"""

    def _at(runs: int) -> bool:
        return MakespanInputs(
            runs=runs,
            agent_minutes=1.0,
            other_minutes=1.0,
            agent_limit=10,
            sandbox_limit=4,
            worker_slots=8,
        ).saturates_slots

    assert _at(16)
    assert not _at(15)


def test_max_agent_minutes_is_zero_when_one_task_alone_busts_the_budget() -> None:
    """一道题自己就跑了七小时的话，`A` 调到 0 也压不进 6 小时。"""
    inputs = MakespanInputs(
        runs=TARGET_RUNS,
        agent_minutes=1.0,
        other_minutes=0.1,
        agent_limit=10,
        sandbox_limit=4,
        worker_slots=8,
        longest_task_minutes=7 * 60,
    )
    assert max_agent_minutes(inputs) == 0.0


def test_longest_task_minutes_rejects_negative() -> None:
    with pytest.raises(ValueError, match="longest_task_minutes"):
        MakespanInputs(
            runs=4,
            agent_minutes=1.0,
            other_minutes=1.0,
            agent_limit=10,
            sandbox_limit=4,
            longest_task_minutes=-1.0,
        )
