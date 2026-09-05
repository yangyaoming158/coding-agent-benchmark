"""处理函数的注册表和它们拿到的上下文（E5-T1）。

## 处理函数的契约

一个处理函数就是 `(JobContext) -> None`。它要遵守三条：

1. **长活儿不要放在事务里。** 一次评测十几分钟，事务开那么久会一直占着连接、
   挡住 vacuum。正确的顺序是：先在事务外把活干完，最后用 `ctx.complete()`
   把结果和"作业已完成"一起提交。
2. **正常返回 = 作业成功。** 哪怕评测结论很难看（比如 `ENV_BUILD_FAILED`）也算成功——
   那是评测层面的事，评测的重试是**另投一条作业**，不是把这条作业重来。
   见 `app.infrastructure.queue.finish` 的注释。
3. **抛异常 = 作业失败。** Worker 会按退避重排，重排次数用完就标 FAILED。
   抛异常的正确场合是"平台自己出问题了，重来一次可能就好了"，
   比如制品存储读不出来、数据库连不上。

## 为什么收尾要由处理函数来做

因为落库和"标记作业完成"必须在**同一个事务**里。分开提交会出现两种中间态：

- 先提交结果、再标作业完成：中间崩了的话，作业被重新领走，同一道题会落两条记录；
- 先标作业完成、再提交结果：中间崩了的话，作业没了，结果也没写，这道题凭空消失。

Worker 没法替处理函数决定"结果"是什么，所以这个事务只能由处理函数发起。
`ctx.complete(write=...)` 就是那个口子：把要写的东西塞给它，它保证和收尾同事务。

处理函数要是压根没什么可写的（或者忘了调），Worker 会在它返回之后补一次
`ctx.complete()`，作业不会挂在 LEASED 上。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import JobState, JobType
from app.infrastructure import queue
from app.infrastructure.config import Settings
from app.infrastructure.db import session_scope

#: 处理函数往事务里写业务结果的回调。拿到的 session 会在返回后连同
#: "把作业标成 DONE" 一起提交。
WriteCallback = Callable[[Session], None]


class JobContext:
    """一条作业交给处理函数时带上的全部东西。"""

    def __init__(
        self,
        *,
        job_id: int,
        job_type: JobType,
        payload: Mapping[str, Any],
        attempts: int,
        worker_id: str,
        settings: Settings,
        session_factory: sessionmaker[Session],
        stop_event: threading.Event | None = None,
    ) -> None:
        self.job_id = job_id
        self.job_type = job_type
        self.payload = payload
        #: 这条作业**被领取过**几次（含这次）。>1 说明上一次 Worker 没能正常收尾。
        self.attempts = attempts
        self.worker_id = worker_id
        self.settings = settings
        self.session_factory = session_factory
        self._stop = stop_event or threading.Event()
        self._completed = False

    @property
    def completed(self) -> bool:
        """这条作业是不是已经收过尾了。"""
        return self._completed

    def should_stop(self) -> bool:
        """Worker 收到停机信号了吗。

        处理函数可以在阶段之间查一下，早点收手。**不是抢占式的**——
        查不查、什么时候查由处理函数自己决定，没人会打断它跑到一半的容器。
        """
        return self._stop.is_set()

    def complete(
        self, write: WriteCallback | None = None, *, state: JobState = JobState.DONE
    ) -> None:
        """在**同一个事务**里写业务结果、把作业标成完成，然后提交。

        `write` 拿到的 session 和收尾用的是同一个。它抛异常的话整个事务回滚，
        作业还留在 LEASED 上，由 Worker 按失败路径处理。

        租约已经不归自己了会抛 `LeaseLostError`，事务同样回滚 —— 这次的结果
        必须丢掉，因为已经有另一个 Worker 在重跑这条作业了。
        """
        with session_scope(self.session_factory) as session:
            if write is not None:
                write(session)
            queue.finish(session, job_id=self.job_id, worker_id=self.worker_id, state=state)
        self._completed = True


#: 一个作业处理函数。
JobHandler = Callable[[JobContext], None]


class HandlerRegistry:
    """`JobType → 处理函数` 的字典。

    做成对象而不是模块级字典，是为了让测试能塞一个假的处理函数进去，
    不用去改全局状态（改了忘恢复，下一条测试就会莫名其妙地用到它）。
    """

    def __init__(self, handlers: Mapping[JobType, JobHandler] | None = None) -> None:
        self._handlers: dict[JobType, JobHandler] = dict(handlers or {})

    def register(self, job_type: JobType, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    def get(self, job_type: JobType) -> JobHandler | None:
        return self._handlers.get(job_type)

    @property
    def job_types(self) -> tuple[JobType, ...]:
        """这个 Worker 能处理哪几种作业。

        领取时按它过滤，**不领自己处理不了的**。不过滤的话，一个只会跑评测的
        Worker 会把建镜像的作业领走然后失败，那条作业的重试次数就这么被耗光了。
        """
        return tuple(self._handlers)


__all__ = ["HandlerRegistry", "JobContext", "JobHandler", "WriteCallback"]
