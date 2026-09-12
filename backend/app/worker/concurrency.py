"""双层并发信号量（E5-T2，ADR-012）。

    ┌─ Worker 进程 ───────────────────────────────────────────┐
    │  agent_sem   = Semaphore(agent_concurrency)    默认 10   │
    │  sandbox_sem = Semaphore(sandbox_concurrency)  默认 4    │
    │                                                          │
    │  槽位 1  [物化]───[  调 AI  ]────────[跑测试]            │
    │           sandbox     agent            sandbox           │
    │  槽位 2      [物化]──[   调 AI   ]───────[跑测试]        │
    │  ...                                                     │
    └──────────────────────────────────────────────────────────┘

## 为什么要两个数字而不是一个

被测 AI 干活的时候基本都在等大模型返回，本机 CPU 空着；跑测试的时候是实打实
吃 CPU 和内存。用一个数字管这两件事，要么浪费吞吐（按内存设成 4，AI 那边只能跑 4 个），
要么撑爆内存（按吞吐设成 12，12 个测试容器 × 1.3 GB 直接把 11 GB 吃穿）。
需求 §4.6 的原话是"对外声明的并行度 = 同时处于已开始但还没结束状态的评测任务数"，
所以在途任务数由槽位数决定，两把信号量只管"这一刻允许几个在做哪种活"。

## 为什么信号量在 Worker 进程里，不在数据库里

§15.2 定的就是进程内两把。做成跨进程的分布式信号量要额外的租约、心跳和崩溃回收
（等于再写一遍队列），而真正要保护的是**这台机器的内存**——一台机器上跑一个
Worker 进程就够了，多开进程的场景本来就要重新算内存账。

## 内存刹车（E9-T2）

信号量管的是"几个在跑"，管不了"跑起来会不会把内存吃穿"：同样是 4 个测试容器，
跑 click 的测试占几百 MB，跑一道按上限吃满的题就是 6 GB。所以拿到沙箱名额之后
还要再过一道：宿主的 `MemAvailable` 低于 `sandbox_min_available_mb` 就先等着。

**为什么在拿到名额之后等，不在之前等**：在之前等的话，所有排队的线程都会
盯着内存空转；在之后等，最多只有 `sandbox_limit` 个线程在等，而且它们占着名额，
等于顺带把后面的人也拦住了 —— 这正是想要的节流。

**为什么等不到也要放行**（`sandbox_memory_wait_timeout_s`）：内存不一定是我们占的。
真有个别的进程吃满了内存，死等会让整个 Worker 停摆，而它本来还能把手上这道题跑完。

## 取消怎么插进来

等名额可能等很久（沙箱名额只有 4 个）。所以不用 `sem.acquire()` 死等，
而是每 `poll_s` 秒醒一次看看实验是不是被取消了，被取消就抛 `TaskCancelledError`。
死等的话，取消一次实验要等到最后一个排队的 task 拿到名额才停得下来，
30 秒的验收标准根本达不到。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from app.domain.capacity import room_for_container
from app.evaluation.gate import TaskCancelledError
from app.infrastructure.config import Settings
from app.infrastructure.hostmem import read_host_memory
from app.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: 等名额时多久醒一次看看有没有被取消（秒）。
DEFAULT_POLL_S = 1.0

#: 读一次宿主可用内存（MiB）。读不到返回 None —— 那就当刹车不存在，照跑。
MemoryReader = Callable[[], int | None]


def _default_memory_reader() -> int | None:
    memory = read_host_memory()
    return None if memory is None else memory.available_mb


#: 等名额等了这么久才值得记一条日志（秒）。低于这个数的排队是正常现象，
#: 每次都记会把日志刷满，真正的长时间阻塞反而看不见了。
_WAIT_LOG_THRESHOLD_S = 5.0


class ConcurrencyLimits:
    """一个 Worker 进程内的两把信号量。线程安全。

    `gate_for()` 给每条在跑的作业发一个 `SlotGate`，闸门背后共用同一对信号量。
    """

    def __init__(
        self,
        *,
        agent: int,
        sandbox: int,
        poll_s: float = DEFAULT_POLL_S,
        min_available_mb: int = 0,
        memory_wait_timeout_s: float = 120.0,
        memory_reader: MemoryReader | None = None,
    ) -> None:
        if agent < 1 or sandbox < 1:
            raise ValueError(f"两层并发都至少是 1，收到 agent={agent}、sandbox={sandbox}")
        self.agent_limit = agent
        self.sandbox_limit = sandbox
        self.poll_s = poll_s
        #: 低于这个可用内存就先别开新的测试容器（MiB）。0 = 关掉刹车。
        self.min_available_mb = min_available_mb
        self.memory_wait_timeout_s = memory_wait_timeout_s
        self._read_available_mb: MemoryReader = memory_reader or _default_memory_reader
        self._agent_sem = threading.Semaphore(agent)
        self._sandbox_sem = threading.Semaphore(sandbox)
        self._lock = threading.Lock()
        self._in_use = {"agent": 0, "sandbox": 0}

    @classmethod
    def from_settings(cls, settings: Settings) -> ConcurrencyLimits:
        """按配置里的 `AGENT_CONCURRENCY` / `SANDBOX_CONCURRENCY` 建。"""
        return cls(
            agent=settings.agent_concurrency,
            sandbox=settings.sandbox_concurrency,
            min_available_mb=settings.sandbox_min_available_mb,
            memory_wait_timeout_s=settings.sandbox_memory_wait_timeout_s,
        )

    def gate_for(self, cancel: threading.Event | None = None) -> SlotGate:
        """给一条作业发一个闸门。`cancel` 置位后，它的等待和阶段检查都会抛 `TaskCancelledError`。"""
        return SlotGate(self, cancel or threading.Event())

    def in_use(self) -> dict[str, int]:
        """当前两层各自占了几个名额。只用来写日志和排查，不参与调度。"""
        with self._lock:
            return dict(self._in_use)

    # ── 给 SlotGate 用的内部方法 ──

    def _semaphore(self, phase: str) -> threading.Semaphore:
        return self._agent_sem if phase == "agent" else self._sandbox_sem

    def _took(self, phase: str, delta: int) -> int:
        with self._lock:
            self._in_use[phase] += delta
            return self._in_use[phase]


class SlotGate:
    """一条作业手里的阶段闸门。实现 `app.evaluation.gate.PhaseGate`。

    一次只能持有一把名额 —— 这不是靠断言保证的，是靠 `execute_task_run()` 里
    两个 `with` 前后相接、不嵌套（§15.2）。嵌套的话实际并发会变成两者的较小值。
    """

    def __init__(self, limits: ConcurrencyLimits, cancel: threading.Event) -> None:
        self._limits = limits
        self._cancel = cancel

    @property
    def cancel_event(self) -> threading.Event:
        """置位它就等于取消这条作业。取消看门线程拿的就是这个。"""
        return self._cancel

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise TaskCancelledError("实验已取消")

    def sandbox(self) -> AbstractContextManager[None]:
        return self._hold("sandbox")

    def agent(self) -> AbstractContextManager[None]:
        return self._hold("agent")

    @contextmanager
    def _hold(self, phase: str) -> Iterator[None]:
        """占一个名额，用完还回去。等的过程中被取消就抛 `TaskCancelledError`。"""
        self.raise_if_cancelled()
        sem = self._limits._semaphore(phase)
        waited = 0.0
        while not sem.acquire(timeout=self._limits.poll_s):
            waited += self._limits.poll_s
            # 每醒一次看一眼取消标志。死等的话，取消要等到最后一个排队的
            # task 拿到名额才停得下来，30 秒的验收标准就没了
            self.raise_if_cancelled()
        if waited >= _WAIT_LOG_THRESHOLD_S:
            logger.info("phase_slot_waited", phase=phase, waited_s=round(waited, 1))
        if phase == "sandbox":
            try:
                self._wait_for_memory()
            except BaseException:
                sem.release()  # 拿到名额之后才等内存，等出事要把名额还回去
                raise
        used = self._limits._took(phase, 1)
        logger.debug("phase_slot_taken", phase=phase, in_use=used)
        try:
            yield
        finally:
            self._limits._took(phase, -1)
            sem.release()

    def _wait_for_memory(self) -> None:
        """等到宿主有足够可用内存再开测试容器（E9-T2）。

        三种情况直接返回：刹车关着、读不到内存、内存本来就够。
        等超时了也返回，只是多打一条告警 —— 理由见模块文档。
        """
        limits = self._limits
        if limits.min_available_mb <= 0:
            return
        waited = 0.0
        while True:
            available = limits._read_available_mb()
            if available is None or room_for_container(
                available, min_available_mb=limits.min_available_mb
            ):
                if waited:
                    logger.info(
                        "sandbox_memory_brake_released",
                        waited_s=round(waited, 1),
                        available_mb=available,
                    )
                return
            if waited >= limits.memory_wait_timeout_s:
                logger.warning(
                    "sandbox_memory_brake_timeout",
                    waited_s=round(waited, 1),
                    available_mb=available,
                    min_available_mb=limits.min_available_mb,
                    detail="等不到内存也要放行，否则 Worker 会被别人占的内存卡死",
                )
                return
            if waited == 0.0:
                logger.warning(
                    "sandbox_memory_brake_engaged",
                    available_mb=available,
                    min_available_mb=limits.min_available_mb,
                )
            self.raise_if_cancelled()
            time.sleep(limits.poll_s)
            waited += limits.poll_s


__all__ = ["DEFAULT_POLL_S", "ConcurrencyLimits", "SlotGate"]
