"""各类作业的处理函数（E5-T1）。

现在只有 `EVAL_TASK` 一种。其余五种（`VALIDATE_TASK`、`BUILD_IMAGE`、
`ATTRIBUTE`、`MINE_REPO`、`GEN_REPORT`）分别属于 E1-T3、E2-T3、E6-T2、E8-T1、E10-T3，
到时候各自加一个模块、在 `default_registry()` 里加一行。

Worker 只领取自己注册过的作业类型。不过滤的话，一个只会跑评测的 Worker 会把
建镜像的作业领走然后失败，那条作业的重试次数就这么被耗光了。
"""

from app.domain.enums import JobType
from app.worker.handlers.eval_task import (
    EvalTaskPayload,
    PayloadError,
    enqueue_eval_task,
    handle_eval_task,
)
from app.worker.registry import HandlerRegistry


def default_registry() -> HandlerRegistry:
    """这个 Worker 会处理哪些作业。"""
    return HandlerRegistry({JobType.EVAL_TASK: handle_eval_task})


__all__ = [
    "EvalTaskPayload",
    "PayloadError",
    "default_registry",
    "enqueue_eval_task",
    "handle_eval_task",
]
