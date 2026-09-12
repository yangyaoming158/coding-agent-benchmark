"""内存容量模型（E9-T2）。

这一层回答的是"这组并发数最坏情况要多少内存"，是个纯算术题，所以放单元测试里。
真实水位是另一回事，由 `python -m cli.stress` 实测，结论回填
`07-platform-architecture.md` §18.5。

有一条测试是**钉现状**的（`test_shipped_defaults_worst_case_is_the_documented_number`）：
仓库里那组默认值在这台机器上是什么结论，文档里怎么写的，它就断言什么。
改默认值会让它红 —— 那时候应该连文档一起改，而不是把断言改掉。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.capacity import (
    DEFAULT_AGENT_MEMORY_MB,
    DEFAULT_SANDBOX_MEMORY_MB,
    SAFE_MEMORY_RATIO,
    ConcurrencyPlan,
    assess,
    max_sandbox_limit,
    room_for_container,
)
from app.domain.execution_plan import ExecutionPlan
from app.infrastructure.config import Settings
from app.sandbox.container import ResourceLimits, agent_limits

#: 这台开发机的口径（`01-requirements.md` §4.6）：WSL 拿到 11.7 GiB。
HOST_TOTAL_MB = 11960
#: 基线：数据库 + dockerd + Worker 自己 + WSL 内核开销。2026-09-12 实测 2.2 GB，
#: 开着编辑器和前端会更高，这里按 §4.6 写的 3.2 GB 算，留一点余量。
HOST_BASELINE_MB = 3200


def _shipped() -> Settings:
    """仓库里那组默认值。

    `_env_file=None` 是必须的：开发机根目录有 `.env`，不屏蔽的话这条测试断言的是
    "这台机器现在配了什么"，而不是"仓库发出去的默认值是什么"。
    """
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _dataclass_default(cls: type, field_name: str) -> object:
    """取 dataclass 字段的默认值。`slots=True` 的类不能直接从类属性上读到。"""
    return next(f.default for f in dataclasses.fields(cls) if f.name == field_name)


# ── 最坏情况怎么数容器 ──────────────────────────────────────


def test_worst_case_counts_agent_containers_too() -> None:
    """8 个槽位 = 最坏情况 8 个容器，其中 5 个测试 + 3 个 Agent。

    §4.6 原来那笔账只算了 `5 × 1.5 GB`，把 Agent 阶段的容器漏掉了 ——
    漏掉的这一半正好是这张卡的起点（E9-T2）。
    """
    plan = ConcurrencyPlan(
        agent_limit=10,
        sandbox_limit=5,
        worker_slots=8,
        agent_memory_mb=1024,
        sandbox_memory_mb=1536,
    )
    assert plan.container_ceiling == 8
    assert plan.worst_case_split == (5, 3)
    assert plan.worst_case_container_mb == 5 * 1536 + 3 * 1024


def test_slots_are_the_real_ceiling() -> None:
    """槽位少于两层之和时，容器数由槽位决定 —— 多出来的名额没人用得上。"""
    plan = ConcurrencyPlan(agent_limit=10, sandbox_limit=5, worker_slots=3)
    assert plan.container_ceiling == 3
    assert plan.worst_case_split == (3, 0)


def test_the_expensive_kind_fills_up_first() -> None:
    """Agent 容器比测试容器贵的时候，最坏情况是 Agent 那边占满。

    不按"哪种贵占哪种"算的话，给出的就不是上界，而上界算小了等于没算。
    """
    plan = ConcurrencyPlan(
        agent_limit=4,
        sandbox_limit=5,
        worker_slots=6,
        agent_memory_mb=2048,
        sandbox_memory_mb=512,
    )
    assert plan.worst_case_split == (2, 4)
    assert plan.worst_case_container_mb == 2 * 512 + 4 * 2048


@pytest.mark.parametrize("bad", [{"agent_limit": 0}, {"sandbox_limit": -1}, {"worker_slots": 0}])
def test_a_plan_must_have_positive_limits(bad: dict[str, int]) -> None:
    kwargs = {"agent_limit": 2, "sandbox_limit": 2, "worker_slots": 2, **bad}
    with pytest.raises(ValueError, match="至少是 1"):
        ConcurrencyPlan(**kwargs)


# ── 核算 ────────────────────────────────────────────────────


def test_a_plan_that_fits_says_so() -> None:
    plan = ConcurrencyPlan(
        agent_limit=2,
        sandbox_limit=2,
        worker_slots=2,
        agent_memory_mb=512,
        sandbox_memory_mb=1024,
    )
    verdict = assess(plan, total_mb=16000, baseline_mb=2000)
    assert verdict.fits
    assert verdict.worst_case_mb == 2000 + 2 * 1024
    assert verdict.over_mb == 0
    assert "在线内" in verdict.explain()


def test_over_budget_reports_how_much_over() -> None:
    plan = ConcurrencyPlan(agent_limit=8, sandbox_limit=8, worker_slots=8)
    verdict = assess(plan, total_mb=HOST_TOTAL_MB, baseline_mb=HOST_BASELINE_MB)
    assert not verdict.fits
    assert verdict.over_mb == verdict.worst_case_mb - verdict.budget_mb
    assert "超线" in verdict.explain()


def test_budget_is_eighty_percent() -> None:
    """验收线来自 §4.6：内存峰值 <80%。"""
    plan = ConcurrencyPlan(agent_limit=1, sandbox_limit=1, worker_slots=1)
    verdict = assess(plan, total_mb=10000, baseline_mb=0)
    assert verdict.budget_mb == 8000
    assert SAFE_MEMORY_RATIO == 0.80


def test_max_sandbox_limit_answers_how_many_fit() -> None:
    """预算 = 80% × 11960 - 3200 ≈ 6368 MB，按 1536 一个装得下 4 个。"""
    plan = ConcurrencyPlan(agent_limit=10, sandbox_limit=8, worker_slots=8)
    assert max_sandbox_limit(plan, total_mb=HOST_TOTAL_MB, baseline_mb=HOST_BASELINE_MB) == 4


def test_max_sandbox_limit_never_exceeds_the_plan() -> None:
    """预算再大也不会建议超过已经配的那个数 —— 它回答的是"能不能装下"。"""
    plan = ConcurrencyPlan(agent_limit=10, sandbox_limit=3, worker_slots=8)
    assert max_sandbox_limit(plan, total_mb=64000, baseline_mb=2000) == 3


def test_no_room_at_all_returns_zero() -> None:
    """基线已经吃光预算时返回 0：该调的是基线或者内存上限，不是并发数。"""
    plan = ConcurrencyPlan(agent_limit=2, sandbox_limit=2, worker_slots=2)
    assert max_sandbox_limit(plan, total_mb=4000, baseline_mb=3900) == 0


# ── 内存刹车的判据 ──────────────────────────────────────────


def test_the_brake_engages_below_the_threshold() -> None:
    assert not room_for_container(1500, min_available_mb=2048)
    assert room_for_container(2048, min_available_mb=2048)


def test_zero_turns_the_brake_off() -> None:
    """设成 0 = 关掉刹车，再低也放行。留这个口子是给内存充裕的机器和 CI。"""
    assert room_for_container(10, min_available_mb=0)


# ── 几处默认值必须一致 ──────────────────────────────────────


def test_the_default_memory_cap_is_the_same_everywhere() -> None:
    """题目 schema、执行计划、容器限额、容量模型：1536 这个数只能有一份含义。

    四处对不上的话，算出来的最坏情况和真实起容器时用的限额就不是一回事，
    而这种错不会报错，只会让容量结论悄悄失真。
    """
    assert ResourceLimits().memory_mb == DEFAULT_SANDBOX_MEMORY_MB
    assert _dataclass_default(ExecutionPlan, "sandbox_memory_mb") == DEFAULT_SANDBOX_MEMORY_MB
    assert TaskDefinition.model_fields["sandbox_memory_mb"].default == DEFAULT_SANDBOX_MEMORY_MB


def test_agent_stage_limits_come_from_one_place() -> None:
    """Agent 容器的默认限额由 `agent_limits()` 给，两个适配器都调它。"""
    assert agent_limits().memory_mb == DEFAULT_AGENT_MEMORY_MB
    assert agent_limits(memory_mb=777).memory_mb == 777
    assert agent_limits(cpus=2.5).cpus == 2.5
    # 关键点：Agent 容器不再捡测试容器那个 1536
    assert DEFAULT_AGENT_MEMORY_MB < DEFAULT_SANDBOX_MEMORY_MB
    # 配置里的默认值和它是同一个数，改一处不会漏另一处
    assert _shipped().agent_memory_mb == DEFAULT_AGENT_MEMORY_MB


# ── 钉住仓库里那组默认值 ────────────────────────────────────


def test_shipped_defaults_worst_case_is_the_documented_number() -> None:
    """仓库默认值在这台机器上的最坏情况，必须和 §18.5 写的一致。

    这条会因为改默认值而红。红了就去改文档 —— 这正是它存在的理由：
    E9-T2 之前这笔账只存在于 §4.6 的一段文字里，而且算漏了 Agent 容器。
    """
    settings = _shipped()
    plan = ConcurrencyPlan(
        agent_limit=settings.agent_concurrency,
        sandbox_limit=settings.sandbox_concurrency,
        worker_slots=settings.worker_slots,
        agent_memory_mb=settings.agent_memory_mb,
        sandbox_memory_mb=DEFAULT_SANDBOX_MEMORY_MB,
    )
    verdict = assess(plan, total_mb=HOST_TOTAL_MB, baseline_mb=HOST_BASELINE_MB)
    assert plan.worst_case_split == (4, 4)
    assert verdict.worst_case_mb == 3200 + 4 * 1536 + 4 * 1024
    assert not verdict.fits  # 按"所有容器同时吃满上限"算，本机在 8 路在途下必然超线


def test_the_brake_reserves_at_least_one_container() -> None:
    """刹车阈值必须够放下一个测试容器，否则它拦不住真正危险的那一步。"""
    assert _shipped().sandbox_min_available_mb >= DEFAULT_SANDBOX_MEMORY_MB
