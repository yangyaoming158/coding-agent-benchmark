"""内存容量模型（E9-T2）：一组并发数在这台机器上跑不跑得下。

回答一个问题：`agent_concurrency=10 / sandbox_concurrency=4 / worker_slots=8`
这组数，**最坏情况**要多少内存，超没超验收线？

## 两个口径，不要混用

| 口径 | 怎么算 | 用来干什么 |
|:---|:---|:---|
| **硬墙**（本模块） | 各容器**声明上限**之和 + 基线 | 告警、写报告。它是"所有容器同时吃满"的假设 |
| 实测水位 | `scripts/mem_sample.py` 采的 `MemAvailable` | 定档判据 |

为什么不拿硬墙当定档判据：docker 的 `--memory` 是**上限不是预留**。按上限之和定容，
这台 11.7 GB 的机器连 4 路都跑不了，而实测占用只有上限的十分之一
（`benchmark-dev` 的 22 道题，测试阶段平均 4.4 秒、一百多 MB）。

为什么硬墙还是要算：接上真实 Agent 之后，最坏情况是会兑现的那一侧。
`01-requirements.md` §4.6 算过一次 `5 × 1.5 GB + 3.2 GB = 91%`，
但那笔账**只数了测试容器**，漏掉了 Agent 阶段的容器（E9-T2 发现，见 §18.5）。

## 最坏情况怎么数容器

一道题在任何一个时刻**最多占一个容器**：Agent 阶段一个、测试阶段一个，
两个 `with` 前后相接不嵌套（`app/worker/concurrency.py` 的 `SlotGate`）。所以

    容器数 n = min(worker_slots, agent_limit + sandbox_limit)

其中测试容器不超过 `sandbox_limit` 个、Agent 容器不超过 `agent_limit` 个。
最坏情况就是在这个约束下**尽量多占贵的那一种**。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 内存水位的验收线（`01-requirements.md` §4.6：峰值 <80%）。
SAFE_MEMORY_RATIO = 0.80

#: 题目没另外声明时，测试容器的内存上限（MiB）。
#: 和 `03-benchmark-spec.md` §7.1、`ExecutionPlan.sandbox_memory_mb`、
#: `ResourceLimits.memory_mb` 的默认值一致 —— 四处一致有测试钉着
#: （`tests/unit/test_capacity.py`）。
DEFAULT_SANDBOX_MEMORY_MB = 1536

#: Agent 容器的默认内存上限（MiB）和 CPU 配额。
#:
#: 比测试容器小：Agent 阶段绝大部分时间在等大模型返回，不吃内存。E9-T2 之前
#: 这两个数根本不存在，Agent 容器捡的是按测试容器定的 1536 —— 于是内存账里
#: 最大的一块是个没人选过的值（`07-platform-architecture.md` §18.5）。
#:
#: 真正生效的值来自配置（`AGENT_MEMORY_MB` / `AGENT_CPUS`）；这里是配置没给时的兜底，
#: 沙箱层的 `agent_limits()` 用的就是它。
DEFAULT_AGENT_MEMORY_MB = 1024
DEFAULT_AGENT_CPUS = 1.0


@dataclass(frozen=True, slots=True)
class ConcurrencyPlan:
    """一组并发设置，加上两种容器各自的内存上限。"""

    #: 同时允许几个被测 AI 在跑。
    agent_limit: int
    #: 同时允许几个测试容器在跑。
    sandbox_limit: int
    #: 一个 Worker 进程同时在途几道题（= 对外声明的并行度）。
    worker_slots: int
    #: Agent 容器的内存上限（MiB）。
    agent_memory_mb: int = DEFAULT_AGENT_MEMORY_MB
    #: 测试容器的内存上限（MiB）。题目可以逐题声明，这里取数据集里的最大值。
    sandbox_memory_mb: int = DEFAULT_SANDBOX_MEMORY_MB

    def __post_init__(self) -> None:
        for name in ("agent_limit", "sandbox_limit", "worker_slots"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 至少是 1，收到 {getattr(self, name)}")
        for name in ("agent_memory_mb", "sandbox_memory_mb"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 至少是 1，收到 {getattr(self, name)}")

    @property
    def container_ceiling(self) -> int:
        """同一时刻最多有几个容器。"""
        return min(self.worker_slots, self.agent_limit + self.sandbox_limit)

    @property
    def worst_case_split(self) -> tuple[int, int]:
        """最坏情况下的 `(测试容器数, Agent 容器数)`。

        贵的那种优先占满 —— 这样得到的才是上界。两种一样贵时先占测试容器，
        结果一样，只是好读。
        """
        ceiling = self.container_ceiling
        if self.sandbox_memory_mb >= self.agent_memory_mb:
            sandbox = min(ceiling, self.sandbox_limit)
            return sandbox, ceiling - sandbox
        agent = min(ceiling, self.agent_limit)
        return ceiling - agent, agent

    @property
    def worst_case_container_mb(self) -> int:
        """最坏情况下容器一共要多少内存（MiB），不含基线。"""
        sandbox, agent = self.worst_case_split
        return sandbox * self.sandbox_memory_mb + agent * self.agent_memory_mb


@dataclass(frozen=True, slots=True)
class CapacityVerdict:
    """一次容量核算的结论。`fits` 为假不代表跑不了，代表**最坏情况**会超线。"""

    plan: ConcurrencyPlan
    #: 机器总内存（MiB）。
    total_mb: int
    #: 非容器占用（MiB）：数据库、dockerd、Worker 自己、WSL 内核开销。
    baseline_mb: int
    #: 验收线对应的内存（MiB）。
    budget_mb: int

    @property
    def worst_case_mb(self) -> int:
        """最坏情况的总占用：基线 + 全部容器按上限。"""
        return self.baseline_mb + self.plan.worst_case_container_mb

    @property
    def worst_case_pct(self) -> float:
        return self.worst_case_mb * 100.0 / self.total_mb

    @property
    def fits(self) -> bool:
        return self.worst_case_mb <= self.budget_mb

    @property
    def over_mb(self) -> int:
        """超出预算多少（MiB）。没超就是 0。"""
        return max(0, self.worst_case_mb - self.budget_mb)

    def explain(self) -> str:
        """一行人话，给日志和 CLI 用。"""
        sandbox, agent = self.plan.worst_case_split
        shape = (
            f"{sandbox}×{self.plan.sandbox_memory_mb}MB 测试 + "
            f"{agent}×{self.plan.agent_memory_mb}MB Agent"
        )
        state = "在线内" if self.fits else f"超线 {self.over_mb} MB"
        return (
            f"最坏情况 {shape} + 基线 {self.baseline_mb}MB = "
            f"{self.worst_case_mb}MB（{self.worst_case_pct:.0f}%，"
            f"预算 {self.budget_mb}MB）{state}"
        )


def assess(
    plan: ConcurrencyPlan,
    *,
    total_mb: int,
    baseline_mb: int,
    ratio: float = SAFE_MEMORY_RATIO,
) -> CapacityVerdict:
    """核算一组并发数的最坏情况内存。

    `baseline_mb` 要用**实测**的（启动时 `MemTotal - MemAvailable`），不要写死：
    这台机器上开不开编辑器、跑不跑前端，基线差一个多 GB。
    """
    if total_mb < 1:
        raise ValueError(f"total_mb 至少是 1，收到 {total_mb}")
    if not 0 < ratio <= 1:
        raise ValueError(f"ratio 要在 (0, 1] 之间，收到 {ratio}")
    return CapacityVerdict(
        plan=plan,
        total_mb=total_mb,
        baseline_mb=max(0, baseline_mb),
        budget_mb=int(total_mb * ratio),
    )


def max_sandbox_limit(
    plan: ConcurrencyPlan,
    *,
    total_mb: int,
    baseline_mb: int,
    ratio: float = SAFE_MEMORY_RATIO,
) -> int:
    """在硬墙口径下，这台机器最多允许几个测试容器。

    给的是**建议值**，不是强制：返回 0 意味着按上限算一个都放不下，
    那说明该调的是内存上限或者基线，不是并发数。
    """
    budget = int(total_mb * ratio) - max(0, baseline_mb)
    if budget <= 0:
        return 0
    return max(0, min(plan.sandbox_limit, budget // plan.sandbox_memory_mb))


def room_for_container(available_mb: int, *, min_available_mb: int) -> bool:
    """现在够不够开下一个测试容器（内存刹车的判据，E9-T2）。

    判据只看一个数：宿主的 `MemAvailable` 还剩多少。不看"我起了几个容器"——
    这台机器上还有数据库、编辑器、别的项目的容器，Worker 只知道自己那部分。

    `min_available_mb = 0` 等于关掉刹车。
    """
    if min_available_mb <= 0:
        return True
    return available_mb >= min_available_mb


__all__ = [
    "DEFAULT_AGENT_CPUS",
    "DEFAULT_AGENT_MEMORY_MB",
    "DEFAULT_SANDBOX_MEMORY_MB",
    "SAFE_MEMORY_RATIO",
    "CapacityVerdict",
    "ConcurrencyPlan",
    "assess",
    "max_sandbox_limit",
    "room_for_container",
]
