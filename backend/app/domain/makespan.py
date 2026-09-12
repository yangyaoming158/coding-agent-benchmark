"""Makespan 投影模型（E9-T1）：这批实验压不压得进 6 小时。

`07-platform-architecture.md` §18.2 那张表一直是手算的。这个模块把它变成代码，
理由有两个：手算的表改一个假设要重算六行，而且**没人会发现它和现实脱节了**——
E9-T1 实测之后 §18.2 里有三个数是错的（S、`P_agent` 的有效值、损耗系数），
而那张表自己不会报错。

## 模型

makespan 指**从第一道题开始跑到最后一道题结束的总墙钟时间**，不是所有题耗时之和。

    makespan ≥ max( N·A / P_agent , N·S / P_sandbox , 最慢的那一道题 ) × (1 + 损耗)

- `N`：总运行次数（MET-02 的口径是 100 题 × 3 Agent = 300）
- `A`：Agent 阶段平均耗时。**最敏感的变量，而且由外部大模型决定，不由我们决定**
- `S`：其余阶段（准备 + 抓补丁 + 跑测试 + 判定）平均耗时之和
- 损耗：调度、排队、起停 Worker 带来的额外时间，**要用实测反算，不要沿用假设值**

前两项取 `max` 而不是相加，是因为两层并发是并行的：一道题在跑测试的时候，
别的题可以同时在调 AI。

## 第三项"最慢的那一道题"是 E9-T1 探测跑时补的

§18.2 原来的公式只有前两项。那两项是**摊平**的下限，`N` 远大于并发数时才成立：
一道题本身不能被拆开并行，所以 makespan 再小也不会小于最慢那道题的耗时。

探测跑（4 次运行、8 个槽位）撞上了这件事：实测 8.3 分钟，而前两项算出来
只有 2.1 分钟，反算出来的"调度损耗"是 **296%** —— 看起来像这台机器烂得不能用，
其实是那 4 次运行里有一次单独跑了 8.25 分钟，而 8 个槽位大半空着。

对 `N=300` 这一项永远不会成为瓶颈（最慢一道题 ≤ 12 分钟的硬超时，而 Agent 侧是
一百多分钟），所以 §18.2 的结论不受影响。但**拿小批次反算损耗系数时它是必须的**。

另有一条配套判据：`saturates_slots` —— 批次填不满槽位时，反算出来的损耗系数
根本不该用，因为那批运行压根没排过队。

## `P_agent` 的有效值会被槽位封顶

这是 E9-T1 查出来的，§18.2 成文时没注意到：

    P_agent 有效值 = min(agent_concurrency, worker_slots)

一道题在任一时刻只占**一个**槽位（`app/worker/concurrency.py`：物化 → 调 AI →
跑测试，三段前后相接不嵌套）。所以槽位数就是在途题数的上限，而"在跑的 Agent"
是在途题的一个子集。仓库默认值 `agent_concurrency=10 / worker_slots=8` 算出来的
有效值是 **8，不是 10** —— §18.2 表里"已实施"那行写的 `P_agent=10 → 180 分钟`
在仓库默认值下拿不到。

要真拿到 10，得把 `worker_slots` 提到 10（这也正是降级表第三档的动作）。

## 降级判据是**跑之前**定的

`DEGRADATION_BANDS` 这张表在 pilot 开跑之前就定死了，不是看了结果再设计的。
纪律和协议 C-60 同一条：C-60 禁止"根据某一次试跑的结果直接调整门槛"，理由是
"用一次实验的结果反过来调整评判标准，等于让被评的对象决定及格线"。
先写规则、后看数据，规则才有约束力。

改这张表要和改门槛一样慎重，别因为某次实测落在档位边上就去挪边界。
"""

from __future__ import annotations

from dataclasses import dataclass

#: MET-02 的验收线：100 题 × 3 Agent 全量评测 ≤ 6 小时。
TARGET_HOURS = 6.0

#: MET-02 口径下的总运行次数：100 题 × 3 Agent。
TARGET_RUNS = 300

#: §18.2 假设的调度损耗。**只是兜底默认值** —— 有实测就用实测（`measured_overhead()`）。
#: 2026-09-12 实测这台机器远低于它，照它投影会系统性偏悲观。
DEFAULT_OVERHEAD_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class MakespanInputs:
    """投影要的全部输入。时间单位统一用**分钟**，和 §18.2 那张表一致。"""

    #: 总运行次数 `N`。
    runs: int
    #: Agent 阶段平均耗时 `A`（分钟）。
    agent_minutes: float
    #: 其余阶段平均耗时之和 `S`（分钟）。
    other_minutes: float
    #: `AGENT_CONCURRENCY`：同时允许几个被测 AI 在跑。
    agent_limit: int
    #: `SANDBOX_CONCURRENCY`：同时允许几个测试容器在跑。
    sandbox_limit: int
    #: `WORKER_SLOTS`：一个 Worker 同时在途几道题。
    #:
    #: `None` 表示**不封顶**，只给复现 §18.2 原表用 —— 那张表成文时还没发现
    #: 槽位会给 `P_agent` 封顶，直接填真实槽位数复现不出原表的数字。
    #: 算"现在这台机器实际是多少"一定要填真实值。
    worker_slots: int | None = None
    #: 调度损耗系数。
    overhead_ratio: float = DEFAULT_OVERHEAD_RATIO
    #: 这批运行里最慢的那一道题花了多少分钟。见模块开头第三项。
    #:
    #: 0 表示不考虑这一项（复现 §18.2 原表时就该是 0）。投影 300 次时给不给都一样，
    #: 它只在批次小、槽位没填满的时候起作用。
    longest_task_minutes: float = 0.0

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError(f"runs 至少是 1，收到 {self.runs}")
        for name in ("agent_minutes", "other_minutes", "longest_task_minutes"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能是负数，收到 {getattr(self, name)}")
        for name in ("agent_limit", "sandbox_limit"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 至少是 1，收到 {getattr(self, name)}")
        if self.worker_slots is not None and self.worker_slots < 1:
            raise ValueError(f"worker_slots 至少是 1，收到 {self.worker_slots}")
        if self.overhead_ratio < 0:
            raise ValueError(f"overhead_ratio 不能是负数，收到 {self.overhead_ratio}")

    @property
    def effective_agent_limit(self) -> int:
        """真正能同时跑几个被测 AI。见模块开头"会被槽位封顶"那一节。"""
        if self.worker_slots is None:
            return self.agent_limit
        return min(self.agent_limit, self.worker_slots)

    @property
    def effective_sandbox_limit(self) -> int:
        """真正能同时跑几个测试容器。同样被槽位封顶。"""
        if self.worker_slots is None:
            return self.sandbox_limit
        return min(self.sandbox_limit, self.worker_slots)

    @property
    def saturates_slots(self) -> bool:
        """这批运行够不够多，能不能把并发填满。

        判据是"至少两波"：`N ≥ 2 × P_agent`。不够的话槽位大半空着，
        **这批实测反算不出调度损耗** —— 排队根本没发生过。
        探测跑 4 次运行、8 个槽位，反算出来 296%，就是这么来的。
        """
        return self.runs >= 2 * self.effective_agent_limit


@dataclass(frozen=True, slots=True)
class Band:
    """降级表的一档。`max_hours` 为 `None` 表示这是最后一档，没有上界。"""

    key: str
    max_hours: float | None
    action: str


#: 降级判据，**pilot 开跑之前定的**（见模块开头）。按投影出来的 makespan 查表。
#:
#: 档位边界是怎么来的：按 `N=300、P_agent=8、损耗 25%` 反算，
#: 4.7 h 对应 `A=6 分钟`（§18.1 的典型值）、6.0 h 对应 `A=7.68 分钟`（MET-02 的线，
#: 精确值不是 7.7）、7.5 h 对应 `A=9.6 分钟`（把 `worker_slots` 提到 10 之后还能救回来的上限）。
DEGRADATION_BANDS: tuple[Band, ...] = (
    Band(
        key="ok",
        max_hours=4.7,
        action="不降级。MET-02 按字面达标，报告里写明余量还有多少",
    ),
    Band(
        key="tight",
        max_hours=TARGET_HOURS,
        action=(
            "不降级，但报告要写明余量只剩多少分钟；把 worker_slots 提到 10 作为已经备好的后手写进去"
        ),
    ),
    Band(
        key="raise_slots",
        max_hours=7.5,
        action=(
            "先用本机能做的：worker_slots 8 → 10，让 P_agent 真到 10，Agent 侧省两成。"
            "改完重跑一轮，确认内存水位仍在 80% 线内（测试容器那一侧不动，还是 4 个）"
        ),
    ),
    Band(
        key="change_terms",
        max_hours=None,
        action=(
            "换口径交付：按 Agent 分三个 2 小时时段（§4.6 备用方案），"
            "或降到 100×2 + 30×1（R12 的兜底）"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Projection:
    """一次投影的结论。"""

    inputs: MakespanInputs

    @property
    def agent_side_minutes(self) -> float:
        """Agent 侧的理论下限：`N·A / P_agent`。"""
        return self.inputs.runs * self.inputs.agent_minutes / self.inputs.effective_agent_limit

    @property
    def sandbox_side_minutes(self) -> float:
        """Sandbox 侧的理论下限：`N·S / P_sandbox`。"""
        return self.inputs.runs * self.inputs.other_minutes / self.inputs.effective_sandbox_limit

    @property
    def single_task_floor_minutes(self) -> float:
        """一道题不能拆开并行，所以最慢那道题的耗时本身就是下限。"""
        return self.inputs.longest_task_minutes

    @property
    def theoretical_minutes(self) -> float:
        """三项取大。前两项是摊平的下限，第三项是"一道题拆不开"的下限。"""
        return max(
            self.agent_side_minutes,
            self.sandbox_side_minutes,
            self.single_task_floor_minutes,
        )

    @property
    def projected_minutes(self) -> float:
        """加上调度损耗之后的投影值。"""
        return self.theoretical_minutes * (1 + self.inputs.overhead_ratio)

    @property
    def projected_hours(self) -> float:
        return self.projected_minutes / 60

    @property
    def bottleneck(self) -> str:
        """哪一项是瓶颈。

        并列时优先报 Agent 侧 —— 那边才是我们管不了的那一侧，也是要提醒人的那一侧。
        报 `single_task` 意味着这批活太少，根本没用上并发。
        """
        if self.agent_side_minutes >= max(
            self.sandbox_side_minutes, self.single_task_floor_minutes
        ):
            return "agent"
        if self.sandbox_side_minutes >= self.single_task_floor_minutes:
            return "sandbox"
        return "single_task"

    def fits(self, target_hours: float = TARGET_HOURS) -> bool:
        return self.projected_hours <= target_hours

    def headroom_minutes(self, target_hours: float = TARGET_HOURS) -> float:
        """离验收线还差多少分钟。负数表示已经超线。"""
        return target_hours * 60 - self.projected_minutes

    def band(self) -> Band:
        """查降级表。表在跑之前就定死了，这里只负责查，不负责判断。"""
        for candidate in DEGRADATION_BANDS:
            if candidate.max_hours is None or self.projected_hours <= candidate.max_hours:
                return candidate
        raise AssertionError("降级表最后一档必须是 max_hours=None，不然会查不到")


def project(inputs: MakespanInputs) -> Projection:
    """按 §18.2 的公式投影一次 makespan。"""
    return Projection(inputs=inputs)


def max_agent_minutes(
    inputs: MakespanInputs,
    *,
    target_hours: float = TARGET_HOURS,
) -> float:
    """在其他条件不变的前提下，`A` 最大能到多少还压得进验收线。

    返回 0 表示**不靠 `A` 的那几项就已经超线了**（Sandbox 侧，或者最慢那一道题），
    这时候调 `A` 没有意义 —— 该调的是并发数、题量或者硬超时。

    这个数比投影值更有用：它是"外部大模型慢到什么程度我们还扛得住"的答案，
    而 `A` 恰恰是我们管不了的那个变量。
    """
    budget_minutes = target_hours * 60 / (1 + inputs.overhead_ratio)
    projection = Projection(inputs)
    # 另外两项不受 A 影响：它们超线的话，A 调到 0 也救不回来
    if max(projection.sandbox_side_minutes, projection.single_task_floor_minutes) > budget_minutes:
        return 0.0
    return budget_minutes * inputs.effective_agent_limit / inputs.runs


def measured_overhead(*, actual_minutes: float, theoretical_minutes: float) -> float:
    """拿实测 makespan 反算这台机器的调度损耗系数。

    `theoretical_minutes` 要用**同一批运行**的理论下限算，不能拿 300 次的理论值
    去除 132 次的实测值。

    理论下限算出来是 0 的时候返回 0：Agent 阶段 0 秒、测试也 0 秒的运行
    （比如一次全员失败的 Oracle 跑）没有可以除的分母，这时候损耗系数没有意义。
    """
    if theoretical_minutes <= 0:
        return 0.0
    return max(0.0, actual_minutes / theoretical_minutes - 1)


__all__ = [
    "DEFAULT_OVERHEAD_RATIO",
    "DEGRADATION_BANDS",
    "TARGET_HOURS",
    "TARGET_RUNS",
    "Band",
    "MakespanInputs",
    "Projection",
    "max_agent_minutes",
    "measured_overhead",
    "project",
]
