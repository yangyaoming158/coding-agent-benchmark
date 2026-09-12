"""阶段耗时统计（E9-T1）：从库里算出 `A` 和 `S`，喂给 makespan 模型。

回答一个问题：**一道题的时间花在哪一段了？**

`evaluation_task_runs` 上本来就记着六个时刻（`app/evaluation/concurrency.py` 拿
同样这几列扫并发曲线）。把相邻两个时刻相减就是一段耗时，不用另外埋点、不用采样。

| 这个模块叫的名字 | 区间 | §18.1 里对应哪一行 |
|:---|:---|:---|
| `queue` | `queued_at` → `prepare_started_at` | 没有 —— 排队等槽位，不是干活 |
| `prepare` | `prepare_started_at` → `agent_started_at` | PREPARING |
| `agent` | `agent_started_at` → `agent_finished_at` | AGENT_RUNNING（**就是 `A`**） |
| `handoff` | `agent_finished_at` → `test_started_at` | PATCH_CAPTURED + 等沙箱名额 |
| `test` | `test_started_at` → `test_finished_at` | TESTING |
| `judge` | `test_finished_at` → `completed_at` | JUDGING + ANALYZING |

## `S` 是"总计减掉 Agent"，不是把几段加起来

§18.2 的 `S` 要的是"除了调 AI 以外的所有时间"。逐段相加会漏掉段与段之间的缝
（等沙箱名额、线程切换），而 makespan 模型关心的是墙钟，漏掉的缝是真实存在的时间。
所以 `S = 单题总计 − Agent 阶段`，一个减法，漏不掉东西。

`queue` 不算进 `S`：排队是**并发不够**的结果，不是一道题的固有成本。把它算进 `S`
再去推"并发开大一点要多久"，等于拿上一次排队的代价去预测下一次 —— 并发开大之后
那段排队本来就该缩短。单独列出来看就行。

## 统计口径：全部 attempt，不只 canonical

一次超时重试实打实占了机器，哪怕它最后不是 canonical attempt（协议 C-24）。
makespan 关心的是机器忙了多久，所以分母是**全部** attempt。
解决率那边的口径不一样（C-21 只数 canonical），两个数不该用同一个分母。

## 百分位用最近秩，不插值

§18.4 的原话是"把所有任务按耗时排序后第 95% 那个值"，那就是最近秩
（nearest-rank），不是线性插值。两种算法在样本少的时候能差出几十秒，
而 pilot 一个 Agent 只有几十个样本。选了哪种要写下来，不然下次有人换一种算法，
报表上的数变了却没人知道为什么。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import ceil

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import InfraOutcome
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun

#: 阶段的名字和顺序，也是 CSV 的列名。`total` 是单题总计，`other` 就是 `S`。
STAGES = ("queue", "prepare", "agent", "handoff", "test", "judge", "total", "other")

#: 每个阶段一句人话，给 CLI 打表头用。
STAGE_MEANINGS: dict[str, str] = {
    "queue": "排队等槽位（并发不够的结果，不算进 S）",
    "prepare": "物化工作区 + 起容器",
    "agent": "被测 AI 干活 —— 这一列就是 A",
    "handoff": "抓补丁 + 等沙箱名额",
    "test": "跑 F2P ∪ P2P",
    "judge": "判定 + 归因",
    "total": "单题总计（prepare → completed）",
    "other": "S = total − agent",
}


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次执行的阶段耗时，单位**秒**。取不到的阶段是 `None`。"""

    task_run_id: int
    evaluation_run_id: int
    agent_name: str
    task_id: str
    attempt_no: int
    infra_outcome: InfraOutcome | None
    started_at: datetime | None
    finished_at: datetime | None
    stages: dict[str, float | None]

    @property
    def timed_out(self) -> bool:
        """Agent 阶段撞上硬超时。它往 `A` 里塞满一个 `agent_timeout_s`。"""
        return self.infra_outcome == InfraOutcome.AGENT_TIMEOUT


@dataclass(frozen=True, slots=True)
class StageStats:
    """一个阶段的耗时分布，单位秒。"""

    stage: str
    samples: int
    mean_s: float
    p50_s: float
    p95_s: float
    max_s: float


@dataclass(frozen=True, slots=True)
class AgentTiming:
    """一个 Agent 在这批运行里的耗时画像。"""

    agent_name: str
    attempts: int
    timeouts: int
    stats: dict[str, StageStats]

    @property
    def timeout_rate(self) -> float:
        """超时占比。`A` 最大的单一来源，混在均值里看不出来，所以单列。"""
        return 0.0 if self.attempts == 0 else self.timeouts / self.attempts

    @property
    def agent_minutes(self) -> float:
        """`A`，分钟。"""
        return self._mean_minutes("agent")

    @property
    def other_minutes(self) -> float:
        """`S`，分钟。"""
        return self._mean_minutes("other")

    def _mean_minutes(self, stage: str) -> float:
        found = self.stats.get(stage)
        return 0.0 if found is None else found.mean_s / 60


def percentile(values: Sequence[float], fraction: float) -> float:
    """最近秩百分位。`fraction=0.95` 就是 §18.4 要的 P95。

    空序列返回 0.0 —— 调用方已经按 `samples` 过滤过了，这里不抛异常是为了
    让"一个阶段没有样本"不至于让整张报表打不出来。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize_stage(stage: str, values: Sequence[float]) -> StageStats:
    """把一组耗时聚成一行统计。"""
    return StageStats(
        stage=stage,
        samples=len(values),
        mean_s=sum(values) / len(values) if values else 0.0,
        p50_s=percentile(values, 0.50),
        p95_s=percentile(values, 0.95),
        max_s=percentile(values, 1.0),
    )


def summarize(attempts: Sequence[Attempt]) -> list[AgentTiming]:
    """按 Agent 分组聚合。顺序按 Agent 名字，好让两次输出能对着看。"""
    grouped: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.agent_name, []).append(attempt)

    result = []
    for agent_name in sorted(grouped):
        rows = grouped[agent_name]
        stats = {}
        for stage in STAGES:
            values = [
                value for row in rows if (value := row.stages.get(stage)) is not None and value >= 0
            ]
            if values:
                stats[stage] = summarize_stage(stage, values)
        result.append(
            AgentTiming(
                agent_name=agent_name,
                attempts=len(rows),
                timeouts=sum(1 for row in rows if row.timed_out),
                stats=stats,
            )
        )
    return result


def total_stage_minutes(attempts: Sequence[Attempt], stage: str) -> float:
    """这批运行在某个阶段上**一共**花了多少分钟（机器工时，不是墙钟）。

    反算调度损耗要用这个，不能用"均值 × 次数"。理由：pilot 这一批是**混合负载**
    （aider 的 Agent 阶段均值 50 秒、claude-code 79 秒、Oracle 0 秒），
    而投影用的 `A` 取的是最慢那个 Agent。拿最慢的均值去乘总次数，算出来的
    "理论下限"比这批真实干的活还多，于是反算出来的损耗是**负数**，被钳到 0 ——
    看起来像这台机器零调度开销，其实是分子分母口径不一致。

    2026-09-12 实测：按均值 × 次数算是 18.0 分钟（比实测的 14.2 还大），
    按真实工时求和算是 11.8 分钟，反算出来 19.9% —— 后者才是这台机器的真实损耗。
    """
    return sum(value for row in attempts if (value := row.stages.get(stage)) is not None) / 60


def actual_makespan_minutes(attempts: Sequence[Attempt]) -> float | None:
    """这批运行实际用了多少分钟墙钟：最后一个结束的时刻减最早开始的时刻。

    和 `evaluation_runs.makespan_ms` 的口径一致（`app/evaluation/progress.py`
    的 `_makespan_ms`），区别是这里跨多个实验算一个总数 —— pilot 是 22 题 ×
    2 个 Agent × 多轮，那几个实验是**连着跑**的，分开算每个实验的 makespan
    没法回答"这批活一共占了机器多久"。
    """
    starts = [row.started_at for row in attempts if row.started_at is not None]
    ends = [row.finished_at for row in attempts if row.finished_at is not None]
    if not starts or not ends:
        return None
    return (max(ends) - min(starts)).total_seconds() / 60


def load_attempts(session: Session, evaluation_run_ids: Sequence[int]) -> list[Attempt]:
    """把这几个实验的全部执行连同阶段耗时读出来。"""
    if not evaluation_run_ids:
        return []

    rows = session.execute(
        sa.select(EvaluationTaskRun, Agent.name, BenchmarkTask.task_id)
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .where(EvaluationTaskRun.evaluation_run_id.in_(evaluation_run_ids))
        .order_by(EvaluationTaskRun.id)
    ).all()

    return [
        Attempt(
            task_run_id=task_run.id,
            evaluation_run_id=task_run.evaluation_run_id,
            agent_name=agent_name,
            task_id=task_id,
            attempt_no=task_run.attempt_no,
            infra_outcome=task_run.infra_outcome,
            started_at=task_run.prepare_started_at,
            finished_at=task_run.completed_at,
            stages=_stages_of(task_run),
        )
        for task_run, agent_name, task_id in rows
    ]


def _stages_of(row: EvaluationTaskRun) -> dict[str, float | None]:
    """把六个时刻列变成八段耗时（秒）。

    `agent` 和 `test` 优先用已经落库的 `*_duration_ms` —— 那是适配器和沙箱层
    自己测的，比两个时刻相减更贴近"容器真的跑了多久"。时刻列只在没有 duration 时兜底。

    兜底用的是 `_first_not_none` 而不是 `a or b`：Oracle 的 Agent 阶段是 **0 毫秒**，
    `0.0 or b` 会取 b，于是一个真实的 0 被当成"没测到"，悄悄换成两个时刻之差。
    """
    agent_s = _first_not_none(
        _ms_to_s(row.agent_duration_ms), _gap(row.agent_started_at, row.agent_finished_at)
    )
    test_s = _first_not_none(
        _ms_to_s(row.test_duration_ms), _gap(row.test_started_at, row.test_finished_at)
    )
    total_s = _first_not_none(
        _ms_to_s(row.total_duration_ms), _gap(row.prepare_started_at, row.completed_at)
    )

    other_s = None if total_s is None else total_s - (agent_s if agent_s is not None else 0.0)

    return {
        "queue": _gap(row.queued_at, row.prepare_started_at),
        "prepare": _gap(row.prepare_started_at, row.agent_started_at),
        "agent": agent_s,
        "handoff": _gap(row.agent_finished_at, row.test_started_at),
        "test": test_s,
        "judge": _gap(row.test_finished_at, row.completed_at),
        "total": total_s,
        "other": other_s,
    }


def _first_not_none(*values: float | None) -> float | None:
    """第一个不是 `None` 的值。0.0 是合法取值，所以不能用 `or` 链。"""
    for value in values:
        if value is not None:
            return value
    return None


def _ms_to_s(value: int | None) -> float | None:
    """毫秒转秒。`0` 要原样返回 0.0 —— Oracle 的 Agent 阶段就是 0，不是"没测到"。"""
    return None if value is None else value / 1000


def _gap(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def to_csv(attempts: Sequence[Attempt]) -> str:
    """逐次执行一行，阶段耗时按秒。给 pilot 的产物归档用。"""
    buffer = io.StringIO()
    header = ["task_run_id", "evaluation_run_id", "agent", "task_id", "attempt_no", "infra_outcome"]
    buffer.write(",".join([*header, *STAGES]) + "\n")
    for row in attempts:
        cells = [
            str(row.task_run_id),
            str(row.evaluation_run_id),
            row.agent_name,
            row.task_id,
            str(row.attempt_no),
            row.infra_outcome.value if row.infra_outcome else "",
        ]
        for stage in STAGES:
            value = row.stages.get(stage)
            cells.append("" if value is None else f"{value:.3f}")
        buffer.write(",".join(cells) + "\n")
    return buffer.getvalue()


__all__ = [
    "STAGES",
    "STAGE_MEANINGS",
    "AgentTiming",
    "Attempt",
    "StageStats",
    "actual_makespan_minutes",
    "load_attempts",
    "percentile",
    "summarize",
    "summarize_stage",
    "to_csv",
    "total_stage_minutes",
]
