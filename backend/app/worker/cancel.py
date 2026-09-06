"""取消看门线程（E5-T2）。

    cli.experiment cancel ──▶ evaluation_runs.status = CANCELLED
                              还没领走的作业标成 DEAD
                                        │
                                        │ 已经在跑的那些呢？
                                        ▼
    Worker ── 每 5 秒 ──▶ 我手上这几条作业，它们的实验被取消了吗？
                              是 ──▶ ① 置取消标志（阶段边界会抛 TaskCancelled）
                                    ② 杀掉这次实验的容器（打断正在等的那一步）

## 为什么光置标志不够

标志是**协作式**的：`execute_task_run()` 只在阶段边界上查它。而一道题最长的那一段
正好没有边界 —— 被测 AI 在容器里跑十几分钟，`container.wait()` 一直阻塞。
只置标志的话，取消要等到这道题自己跑完才生效，验收标准里的 30 秒根本达不到。

所以还要把容器杀掉：`container.wait()` 立刻返回，适配器收到一个非正常退出，
往上走到下一个阶段边界，那里的标志已经置好了，于是收成 `CANCELLED`。

## 为什么杀容器按标签前缀，不按容器 id

Worker 没有在跑的容器 id —— 那是 `run_in_container()` 内部的局部变量，
它自己会在 `finally` 里删掉。往上传一层 id 就要多一份"谁负责删"的约定，
而每个容器上本来就有 `bench.run_id` 标签，值是 `runs/<实验号>/tasks/...`。
按前缀捞是现成的，而且**顺带把僵尸也带走了**：万一有一条作业的租约已经飘了、
容器还留着，它照样会被这次取消清掉。

## 一条纪律：只 kill 不 remove

删容器是 `run_in_container()` 的 `finally` 的事。这里抢着删，那边紧接着的
`container.reload()` 会撞上 404，一次干净的取消就变成一条 `HARNESS_ERROR`。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import EvaluationRunStatus
from app.evaluation.jobs import run_key_prefix
from app.infrastructure.logging import get_logger
from app.infrastructure.models.evaluation import EvaluationRun

logger = get_logger(__name__)

#: 多久查一次实验状态（秒）。查的是 `id IN (...)` 的主键，几条作业就几个 id，
#: 代价可以忽略；这个数直接决定取消的响应上限，不要往大了调。
DEFAULT_POLL_S = 5.0

#: payload 里表示"这条作业属于哪次实验"的键。只有 EVAL_TASK 有，
#: 别的作业类型（建镜像、跑归因）没有这个键，会被跳过。
RUN_ID_KEY = "evaluation_run_id"


@dataclass(frozen=True, slots=True)
class _Watch:
    evaluation_run_id: int
    cancel: threading.Event


class CancelWatcher:
    """盯着这个 Worker 手上的作业，实验被取消就把它们停掉。

    线程安全：`register` / `unregister` 在主循环里调，`poll_once` 在看门线程里跑。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        poll_s: float = DEFAULT_POLL_S,
        docker_client: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._poll_s = poll_s
        self._docker_client = docker_client
        self._lock = threading.Lock()
        self._watching: dict[int, _Watch] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 登记 ────────────────────────────────────────────────

    def register(self, job_id: int, payload: Mapping[str, Any], cancel: threading.Event) -> None:
        """把一条正在跑的作业纳入监视。payload 里没有实验号就直接忽略。"""
        raw = payload.get(RUN_ID_KEY)
        if raw is None:
            return
        with self._lock:
            self._watching[job_id] = _Watch(int(raw), cancel)

    def unregister(self, job_id: int) -> None:
        with self._lock:
            self._watching.pop(job_id, None)

    @property
    def watching(self) -> int:
        with self._lock:
            return len(self._watching)

    # ── 线程 ────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cancel-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._poll_s)

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                self.poll_once()
            except Exception as exc:
                # 数据库抖一下不该让看门线程死掉 —— 它死了之后取消就再也不生效，
                # 而且没有任何报错，表现是"点了取消没反应"
                logger.warning("cancel_watch_failed", error=f"{type(exc).__name__}: {exc}")

    # ── 一轮检查 ────────────────────────────────────────────

    def poll_once(self) -> list[int]:
        """查一遍，把属于已取消实验的作业停掉，返回这些作业的 id。"""
        with self._lock:
            snapshot = dict(self._watching)
        if not snapshot:
            return []

        run_ids = {watch.evaluation_run_id for watch in snapshot.values()}
        cancelled = self._cancelled_runs(run_ids)
        if not cancelled:
            return []

        stopped: list[int] = []
        for job_id, watch in snapshot.items():
            if watch.evaluation_run_id not in cancelled:
                continue
            if not watch.cancel.is_set():
                logger.warning(
                    "job_cancelled",
                    job_id=job_id,
                    evaluation_run_id=watch.evaluation_run_id,
                )
            watch.cancel.set()
            stopped.append(job_id)

        for run_id in sorted(cancelled):
            self._kill_containers(run_id)
        return stopped

    def _cancelled_runs(self, run_ids: set[int]) -> set[int]:
        with self._session_factory() as session:
            rows = session.execute(
                sa.select(EvaluationRun.id).where(
                    EvaluationRun.id.in_(sorted(run_ids)),
                    EvaluationRun.status == EvaluationRunStatus.CANCELLED,
                )
            ).scalars()
            return set(rows)

    def _kill_containers(self, evaluation_run_id: int) -> None:
        """把这次实验还活着的容器全杀掉。Docker 用不了就只记一条警告。"""
        # 局部 import：`app.sandbox` 会去连 docker daemon，没有 Docker 的环境
        # 不该在 import Worker 的时候就炸
        from app.sandbox.container import kill_containers_by_run_prefix

        try:
            kill_containers_by_run_prefix(
                run_key_prefix(evaluation_run_id), client=self._docker_client
            )
        except Exception as exc:
            logger.warning(
                "cancel_kill_containers_failed",
                evaluation_run_id=evaluation_run_id,
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["DEFAULT_POLL_S", "RUN_ID_KEY", "CancelWatcher"]
