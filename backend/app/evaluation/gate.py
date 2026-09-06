"""阶段闸门：双层并发名额和取消信号的接口（E5-T2）。

一句话：`execute_task_run()` 在三个阶段边界上问一句"我现在能开始了吗"，
问的对象就是这里的 `PhaseGate`。

## 为什么接口在 app.evaluation，实现在 app.worker

信号量是 Worker 进程的资源（`07-platform-architecture.md` §15.2 写的就是
"Worker 进程内两把信号量"），但要卡住的那三个阶段在 `app.evaluation.task_run` 里。
模块依赖方向是 `app.worker → app.evaluation`，反过来不行，所以接口放下面、
实现放上面（`app.worker.concurrency`）。

评测单元本身不需要知道有没有并发这回事：默认的 `NULL_GATE` 什么都不做，
所以现有的单测、沙箱测试和 CLI 一行都不用改。

## 两把名额为什么不能同时持有

`sandbox()` 卡的是物化工作区和跑测试容器这类吃 CPU 和内存的活，
`agent()` 卡的是等大模型返回这类基本不占本机资源的活（需求 §4.6）。
一个 task 在 AGENT 阶段持 agent 名额、在 PREPARING/TESTING 阶段持 sandbox 名额，
**中间必须放开**。同时持有的话实际并发等于两者的较小值，双层就退化成单层了。

## 取消为什么用异常而不是返回值

取消可能发生在任何一个 `with` 里面，包括正卡在信号量上等名额的时候。
用返回值的话每一层调用点都得记得检查，漏一处的表现是"取消了但那道题还在跑"。
异常只要在 `execute_task_run()` 最外层接一次。
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, runtime_checkable


class TaskCancelledError(Exception):
    """这道题所属的实验被取消了。

    由闸门在等名额的时候抛，或者由阶段边界上的 `raise_if_cancelled()` 抛。
    `execute_task_run()` 接住它，收成 `infra_outcome = CANCELLED` 的终态结果 ——
    协议里这是一个合法组合（`lifecycle=CANCELLED / infra=CANCELLED / agent=NULL`），
    不是异常路径。
    """


@runtime_checkable
class PhaseGate(Protocol):
    """评测单元和调度层之间的全部接口。三个方法，没有别的。"""

    def sandbox(self) -> AbstractContextManager[None]:
        """占一个沙箱名额（物化工作区、跑测试容器）。等不到就一直等。"""
        ...

    def agent(self) -> AbstractContextManager[None]:
        """占一个 Agent 名额（调被测 AI）。等不到就一直等。"""
        ...

    def raise_if_cancelled(self) -> None:
        """实验已经被取消就抛 `TaskCancelledError`，否则什么都不做。"""
        ...


class NullGate:
    """不限流也不取消。

    `execute_task_run()` 的默认闸门。单测、`cli.runner`、沙箱测试跑的都是它，
    所以引入双层并发没有改变任何已有调用方的行为。
    """

    def sandbox(self) -> AbstractContextManager[None]:
        return nullcontext()

    def agent(self) -> AbstractContextManager[None]:
        return nullcontext()

    def raise_if_cancelled(self) -> None:
        return None


#: 全局共用的空闸门。它没有状态，不需要每次新建一个。
NULL_GATE: PhaseGate = NullGate()


__all__ = ["NULL_GATE", "NullGate", "PhaseGate", "TaskCancelledError"]
