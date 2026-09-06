"""有效并发时间序列（E5-T2，服务 MET-03 和 ADR-012 的风险栏）。

回答一个问题：**这次实验真跑到了几路并行？**

不是"我们配了 8"，是"从数据看，同一时刻确实有几道题在途"。ADR-012 把
"并行度"定义成"同时处于已开始但还没结束状态的评测任务数"，这个模块就是那句定义
的算法实现。

## 不采样，从时刻列扫出来

`evaluation_task_runs` 上本来就记了每一次执行的五个时刻。每行给出三段区间：

| 曲线 | 区间 | 它回答什么 |
|:---|:---|:---|
| `in_flight` | `prepare_started_at` → `completed_at` | 对外声明的并行度（§4.6） |
| `agent` | `agent_started_at` → `agent_finished_at` | 有几个被测 AI 在同时跑 |
| `sandbox` | `test_started_at` → `test_finished_at` | 有几个测试容器在同时跑 |

拿这些区间做一次扫描线，就得到了并发数随时间变化的阶梯曲线。

**为什么不在 Worker 里每秒采一次样：**

- 采样要新开一张表、一个线程，还要考虑采样点丢了怎么办；
- 采样只覆盖"开着采样器的那次运行"，而扫描线对**已经跑完的实验也能出图**；
- 采样有粒度误差，扫描线是精确的 —— 它算的就是那些时刻本身。

代价是没跑完的执行（Worker 崩了、行没落库）不在图里。这是诚实的：
那次执行确实没有留下任何可核对的时刻。

## 三条曲线摞在一起才说明双层并发有用

典型形状是 `in_flight` 顶在 8、`agent` 也在 8 附近、而 `sandbox` 被压在 5 ——
两层信号量在起作用：AI 那层放开跑，测试那层卡住不让撑爆内存。
一条曲线看不出这件事。
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.infrastructure.models.evaluation import EvaluationTaskRun

#: 三条曲线的名字，也是 CSV 的列名。
CURVES = ("in_flight", "agent", "sandbox")


@dataclass(frozen=True, slots=True)
class Interval:
    """一段左闭右开的区间 `[start, end)`。

    左闭右开不是抠字眼：一道题正好在另一道题结束的那一毫秒开始时，
    并发数应该是 1 不是 2，也不该出现一个假的凹口。
    """

    start: datetime
    end: datetime

    @property
    def valid(self) -> bool:
        return self.end >= self.start


@dataclass(frozen=True, slots=True)
class Point:
    """曲线上的一个变化点。两点之间并发数不变（阶梯图）。"""

    at: datetime
    in_flight: int
    agent: int
    sandbox: int

    def value(self, curve: str) -> int:
        return {"in_flight": self.in_flight, "agent": self.agent, "sandbox": self.sandbox}[curve]


@dataclass(frozen=True, slots=True)
class Summary:
    """一条曲线的两个数：峰值和时间加权的 P50。

    P50 **按时间加权**，不是对变化点取中位数。变化点的疏密和实际持续时间没关系 ——
    一秒钟里挤了 10 个变化点，按点算会让那一秒的权重变成 10 倍。
    MET-03 要的是"这次实验有一半的时间里并发数不低于多少"。
    """

    peak: int
    p50: int
    #: 曲线覆盖的墙钟时长（秒）。
    span_s: float


def sweep(
    in_flight: Sequence[Interval],
    agent: Sequence[Interval] = (),
    sandbox: Sequence[Interval] = (),
) -> list[Point]:
    """扫描线：把三组区间算成一串变化点。

    同一时刻的所有增减**一起结算**再出点，所以"一道题结束、另一道题开始"
    落在同一毫秒时不会出现一个假的凹口。
    """
    deltas: dict[datetime, list[int]] = {}

    def add(intervals: Iterable[Interval], channel: int) -> None:
        for interval in intervals:
            if not interval.valid:
                continue
            deltas.setdefault(interval.start, [0, 0, 0])[channel] += 1
            deltas.setdefault(interval.end, [0, 0, 0])[channel] -= 1

    add(in_flight, 0)
    add(agent, 1)
    add(sandbox, 2)

    points: list[Point] = []
    running = [0, 0, 0]
    for at in sorted(deltas):
        step = deltas[at]
        running = [running[i] + step[i] for i in range(3)]
        points.append(Point(at=at, in_flight=running[0], agent=running[1], sandbox=running[2]))
    return points


def summarize(points: Sequence[Point], curve: str = "in_flight") -> Summary:
    """一条曲线的峰值和时间加权 P50。"""
    if not points:
        return Summary(peak=0, p50=0, span_s=0.0)
    peak = max(p.value(curve) for p in points)
    span_s = (points[-1].at - points[0].at).total_seconds()

    # 每段的 (并发数, 持续秒数)，最后一个点是收尾，本身没有时长
    segments = [
        (points[i].value(curve), (points[i + 1].at - points[i].at).total_seconds())
        for i in range(len(points) - 1)
    ]
    total = sum(duration for _, duration in segments)
    if total <= 0:
        return Summary(peak=peak, p50=peak, span_s=span_s)

    # 按并发数从高到低累加时长，累到一半时的那个值就是时间加权中位数
    cumulative = 0.0
    p50 = 0
    for value, duration in sorted(segments, key=lambda item: -item[0]):
        cumulative += duration
        if cumulative >= total / 2:
            p50 = value
            break
    return Summary(peak=peak, p50=p50, span_s=span_s)


def to_csv(points: Sequence[Point]) -> str:
    """导出成 CSV。时间是 ISO-8601（带时区），画阶梯图直接喂给它就行。"""
    buffer = io.StringIO()
    buffer.write("timestamp," + ",".join(CURVES) + "\n")
    for point in points:
        buffer.write(f"{point.at.isoformat()},{point.in_flight},{point.agent},{point.sandbox}\n")
    return buffer.getvalue()


# ── 从数据库取区间 ──────────────────────────────────────────


def load_intervals(
    session: Session, evaluation_run_ids: Sequence[int]
) -> tuple[list[Interval], list[Interval], list[Interval]]:
    """读出三组区间：在途、AI 在跑、测试在跑。

    可以一次给多个实验号 —— 多轮取样是多个 `EvaluationRun`，但它们跑在同一台机器
    的同一段时间里，要看的是**机器上一共有几路在跑**，所以区间直接并在一起。

    端点缺一个就跳过那一段（例如作业崩在半路、没有 `completed_at`）。
    补一个"现在"进去会凭空造出一段并发，那是编出来的数据。
    """
    rows = session.execute(
        sa.select(
            EvaluationTaskRun.prepare_started_at,
            EvaluationTaskRun.completed_at,
            EvaluationTaskRun.agent_started_at,
            EvaluationTaskRun.agent_finished_at,
            EvaluationTaskRun.test_started_at,
            EvaluationTaskRun.test_finished_at,
        ).where(EvaluationTaskRun.evaluation_run_id.in_(list(evaluation_run_ids)))
    ).all()

    in_flight: list[Interval] = []
    agent: list[Interval] = []
    sandbox: list[Interval] = []
    for prep, done, agent_start, agent_end, test_start, test_end in rows:
        _append(in_flight, prep, done)
        _append(agent, agent_start, agent_end)
        _append(sandbox, test_start, test_end)
    return in_flight, agent, sandbox


def _append(target: list[Interval], start: datetime | None, end: datetime | None) -> None:
    if start is None or end is None or end < start:
        return
    target.append(Interval(start=start, end=end))


def series_for(session: Session, evaluation_run_ids: Sequence[int]) -> list[Point]:
    """一步到位：读区间 + 扫描线。"""
    return sweep(*load_intervals(session, evaluation_run_ids))


__all__ = [
    "CURVES",
    "Interval",
    "Point",
    "Summary",
    "load_intervals",
    "series_for",
    "summarize",
    "sweep",
    "to_csv",
]
