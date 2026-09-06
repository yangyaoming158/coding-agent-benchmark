"""EVAL_TASK 作业的报文格式和投递（E5-T1 建，E5-T2 从 worker 层下沉到这里）。

## 为什么从 `app.worker.handlers` 挪下来

原来这几样东西住在处理函数旁边。E5-T2 的编排层（`app.evaluation.orchestrator`）
也要投作业，而模块依赖方向是 `app.worker → app.evaluation`，编排层去 import
处理函数会被 import-linter 拦下（反向依赖）。

所以拆成两半：**作业长什么样、怎么投**在这里（下层，谁都能用），
**领到之后怎么跑**留在 `app.worker.handlers.eval_task`（上层，只有 Worker 用）。

## payload 里为什么只有外键和编号

题目内容跟着 `benchmark_tasks` 走，payload 里再存一份就有了两个真相。
哪天两边不一致，"这次评测到底跑的是哪个版本的题"就说不清楚了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.domain.enums import JobType
from app.infrastructure import queue
from app.infrastructure.models.job import JobQueue

#: 标准化补丁的制品文件名。`app.evaluation.task_run` 落盘时用的就是它
#: （`f"{run_key}/patch.diff"`），这里靠它反推出上一次 attempt 的补丁在哪 ——
#: 重试要复用同一份补丁（协议 C-54），而重跑命令手里没有上一次的 `ArtifactRef`。
NORMALIZED_PATCH_FILENAME = "patch.diff"


class PayloadError(ValueError):
    """作业的 payload 不合法。投作业的那一方写错了，重试多少次都一样。"""


@dataclass(frozen=True, slots=True)
class EvalTaskPayload:
    """EVAL_TASK 作业的 payload。"""

    evaluation_run_id: int
    benchmark_task_id: int
    attempt_no: int = 1
    #: 上一次 attempt 的 `evaluation_task_runs.id`，第一次跑为 None。
    retry_of_id: int | None = None
    #: 上一次那份标准化补丁的制品 key。有值就走 C-54 的重放路径，不再调 AI。
    reuse_patch_key: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EvalTaskPayload:
        try:
            return cls(
                evaluation_run_id=int(payload["evaluation_run_id"]),
                benchmark_task_id=int(payload["benchmark_task_id"]),
                attempt_no=int(payload.get("attempt_no", 1)),
                retry_of_id=_opt_int(payload.get("retry_of_id")),
                reuse_patch_key=_opt_str(payload.get("reuse_patch_key")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadError(f"EVAL_TASK 的 payload 不合法：{payload!r}（{exc}）") from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "evaluation_run_id": self.evaluation_run_id,
            "benchmark_task_id": self.benchmark_task_id,
            "attempt_no": self.attempt_no,
            "retry_of_id": self.retry_of_id,
            "reuse_patch_key": self.reuse_patch_key,
        }


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def run_key_prefix(evaluation_run_id: int) -> str:
    """一次实验的全部制品和容器标签共用的前缀。

    取消要按它把容器捞出来杀掉（`docker` 的标签过滤只能精确匹配，
    所以是先列出带 bench 标签的容器，再在 Python 里按前缀筛）。
    """
    return f"runs/{evaluation_run_id}/"


def run_key_for(payload: EvalTaskPayload) -> str:
    """制品 key 的前缀，也是容器标签里的 run_id。

    做成确定的一段路径（而不是随机 id），是为了拿着一条 `evaluation_task_runs`
    记录就能推出它的制品在哪，不用先去 `artifacts` 表查一次。
    """
    return (
        f"{run_key_prefix(payload.evaluation_run_id)}"
        f"tasks/{payload.benchmark_task_id}"
        f"/attempt-{payload.attempt_no}"
    )


def normalized_patch_key(*, evaluation_run_id: int, benchmark_task_id: int, attempt_no: int) -> str:
    """某次 attempt 的标准化补丁存在哪个 key 上。

    key 是**算出来的**，不是从 `patch_artifacts` 里读的：那张表存的是 `uri`
    （带 `local://` 前缀和 `.gz` 后缀的物理位置），而重放要的是逻辑 key。
    两者的换算规则属于存储后端，编排层不该知道。
    """
    key = run_key_for(
        EvalTaskPayload(
            evaluation_run_id=evaluation_run_id,
            benchmark_task_id=benchmark_task_id,
            attempt_no=attempt_no,
        )
    )
    return f"{key}/{NORMALIZED_PATCH_FILENAME}"


def enqueue_eval_task(
    session: Session,
    *,
    evaluation_run_id: int,
    benchmark_task_id: int,
    attempt_no: int = 1,
    retry_of_id: int | None = None,
    reuse_patch_key: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    delay_s: float = 0.0,
) -> JobQueue:
    """投一条 EVAL_TASK 作业。**不 commit** —— 事务边界归调用方。

    不 commit 是刻意的：投作业常常要和别的写操作绑在同一个事务里。最典型的是
    评测重试 —— "把这次的结果落库"和"排下一次 attempt"必须一起成功，
    分开提交会出现"结果写了但没人接着重试"。
    """
    payload = EvalTaskPayload(
        evaluation_run_id=evaluation_run_id,
        benchmark_task_id=benchmark_task_id,
        attempt_no=attempt_no,
        retry_of_id=retry_of_id,
        reuse_patch_key=reuse_patch_key,
    )
    return queue.enqueue(
        session,
        job_type=JobType.EVAL_TASK,
        payload=payload.to_payload(),
        priority=priority,
        max_attempts=max_attempts,
        delay_s=delay_s,
    )


__all__ = [
    "NORMALIZED_PATCH_FILENAME",
    "EvalTaskPayload",
    "PayloadError",
    "enqueue_eval_task",
    "normalized_patch_key",
    "run_key_for",
    "run_key_prefix",
]
