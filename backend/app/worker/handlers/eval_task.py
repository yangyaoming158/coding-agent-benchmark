"""EVAL_TASK 作业：跑一道题的一次 attempt，落库，决定要不要再来一次（E5-T1）。

    ┌─ 事务 1（毫秒级）─────────────────────────────────────┐
    │ 读题、读实验配置、读环境规格                            │  ← 只读
    └───────────────────────────────────────────────────────┘
       ↓
      不在事务里：execute_task_run()                            ← 十几分钟
       ↓
    ┌─ 事务 2（毫秒级，ctx.complete 发起）──────────────────┐
    │ 建 attempt 行 → persist_task_run()                     │
    │ → decide_next() → 要重试就投下一条作业                  │
    │              → 不重试就给 canonical 那条打标            │
    │ → 把这条作业标成 DONE                                   │
    └───────────────────── 一次提交 ─────────────────────────┘

## 为什么中间那段不能在事务里

一次评测十几分钟。事务开那么久会一直占着一条连接、挡住 vacuum 回收死元组。
而事务 2 里那几件事必须**一起**成功：结果写了但没人接着重试，这道题就永远停在
一个可重试的故障上；作业标了完成但结果没写，这道题就凭空消失了。

## attempt 行为什么跑完才建

领取的时候就建的话，Worker 被 `kill -9` 会在库里留下一条卡在 `AGENT_RUNNING` 的记录。
接手的 Worker 只有两条路，都不通：复用它就要把状态退回 `PREPARING`（协议 C-32
明令禁止状态回退），新建一条又会撞 `uq_task_run_attempt` 唯一约束。

跑完才建就没这个问题：Worker 死了，库里干干净净，作业退回队列重新领走，
`attempt_no` 还是 1。代价是跑的过程中 `evaluation_task_runs` 里查不到"正在跑"，
但 `SELECT * FROM job_queue WHERE state='LEASED'` 看得到，payload 里就写着是哪道题。

## 重试怎么算，谁来重试

**不是**这条作业重来，而是**投一条新作业**（`attempt_no` 加 1）。协议 C-32 要求
重试新建记录，C-53 要求次数由 C-18 的映射表决定——而 `job_queue.max_attempts`
管的是另一件事（Worker 崩了、处理函数抛异常）。两者混用会让重试预算被放大。

规则本身在 `app.domain.retry`，这里只负责查历史、执行决定。

## C-54：拿到补丁之后的故障，重试不许再调 AI

`TEST_TIMEOUT`、`OOM_KILLED`、`TEST_DISCOVERY_ERROR` 这些挂在测试阶段的故障，
补丁早就在手里了。重新调一次 AI 有两个后果：AI 有随机性，这次重试就变成了
一次新采样（等于变相取最优，C-25 禁止）；而且白花一次钱。

所以重试的作业里带上上一次那份标准化补丁的制品 key，由 `StoredPatchRunner` 重放。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.schema import TaskDefinition
from app.domain.enums import JobType, LifecycleStatus, PatchKind
from app.domain.retry import AttemptRecord, decide_next
from app.evaluation.persistence import persist_task_run
from app.evaluation.task_run import TaskRunInputs, TaskRunOutcome, deadline_ms, execute_task_run
from app.infrastructure import queue
from app.infrastructure.logging import get_logger
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkTask, EnvironmentSpec
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.infrastructure.models.job import JobQueue
from app.runner.adapters.stored import StoredPatchRunner

# 换个名字导入：这个模块里的 `AgentConfig` 是数据库那张表，
# 协议里那个同名的是适配器的 harness 侧配置，两者完全无关，重名会读错
from app.runner.protocol import AgentConfig as AgentRunnerConfig
from app.sandbox.container import build_env
from app.sandbox.mirror import MirrorManager
from app.storage import create_artifact_store
from app.storage.base import ArtifactStore
from app.worker.registry import JobContext

logger = get_logger(__name__)


class PayloadError(ValueError):
    """作业的 payload 不合法。投作业的那一方写错了，重试多少次都一样。"""


@dataclass(frozen=True, slots=True)
class EvalTaskPayload:
    """EVAL_TASK 作业的 payload。

    只装**外键和编号**，不装题目内容。题目内容跟着 `benchmark_tasks` 走，
    payload 里再存一份就有了两个真相，哪天两边不一致，
    "这次评测到底跑的是哪个版本的题"说不清楚。
    """

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


@dataclass(frozen=True, slots=True)
class _Loaded:
    """事务 1 从库里读出来的东西。都是纯数据，出了事务还能用。"""

    task: TaskDefinition
    adapter_class: str
    agent_params: Mapping[str, Any]
    model_name: str
    image: str
    extra_protected_paths: tuple[str, ...]
    agent_timeout_s: int
    repo_name: str


def run_key_for(payload: EvalTaskPayload) -> str:
    """制品 key 的前缀，也是容器标签里的 run_id。

    做成确定的一段路径（而不是随机 id），是为了拿着一条 `evaluation_task_runs`
    记录就能推出它的制品在哪，不用先去 `artifacts` 表查一次。
    """
    return (
        f"runs/{payload.evaluation_run_id}"
        f"/tasks/{payload.benchmark_task_id}"
        f"/attempt-{payload.attempt_no}"
    )


def handle_eval_task(ctx: JobContext) -> None:
    """跑一道题的一次 attempt。这就是注册到 `JobType.EVAL_TASK` 上的处理函数。"""
    payload = EvalTaskPayload.from_payload(ctx.payload)
    store = create_artifact_store(ctx.settings)

    # ── 事务 1：把要用的东西读出来 ──
    with ctx.session_factory() as session:
        loaded = _load(session, payload)

    runner = _build_runner(loaded, payload, store=store)

    # ── 不在事务里：真正干活的十几分钟 ──
    scratch_root = Path(ctx.settings.workspace_root) / run_key_for(payload).replace("/", "_")
    mirrors = MirrorManager(ctx.settings.mirror_root, timeout_s=ctx.settings.git_timeout_s)
    inputs = TaskRunInputs(
        plan=loaded.task.execution_plan(extra_protected_paths=loaded.extra_protected_paths),
        agent_input=loaded.task.agent_task_input(
            deadline_unix_ms=deadline_ms(loaded.agent_timeout_s),
            model=loaded.model_name,
        ),
        mirror_path=mirrors.path_for(loaded.repo_name),
        scratch_dir=scratch_root,
        image=loaded.image,
        agent_timeout_s=loaded.agent_timeout_s,
        run_key=run_key_for(payload),
    )
    try:
        outcome = execute_task_run(
            runner, inputs, store=store, agent_config=_agent_config(ctx, loaded)
        )
    finally:
        # 工作区不留：一次评测两份代码树，几百次跑下来就是几十 GB。
        # 出了事也要删——删不掉只记一条日志，不能让它盖掉真正的失败原因。
        shutil.rmtree(scratch_root, ignore_errors=True)

    logger.info(
        "task_run_finished",
        task_id=loaded.task.task_id,
        attempt_no=payload.attempt_no,
        lifecycle=outcome.lifecycle_status.value,
        infra_outcome=outcome.infra_outcome.value,
        agent_outcome=outcome.verdict.agent_outcome.value
        if outcome.verdict.agent_outcome
        else None,
    )

    # ── 事务 2：落库 + 决定重试 + 收尾，一次提交 ──
    ctx.complete(lambda session: _persist_and_schedule(session, ctx, payload, outcome))


# ── 事务 1 ──────────────────────────────────────────────────


def _load(session: Session, payload: EvalTaskPayload) -> _Loaded:
    """读题、读实验用的 Agent 配置、读环境规格。"""
    run = session.get(EvaluationRun, payload.evaluation_run_id)
    if run is None:
        raise PayloadError(f"找不到 evaluation_run {payload.evaluation_run_id}")
    task_row = session.get(BenchmarkTask, payload.benchmark_task_id)
    if task_row is None:
        raise PayloadError(f"找不到 benchmark_task {payload.benchmark_task_id}")

    config = session.get(AgentConfig, run.agent_config_id)
    if config is None:
        raise PayloadError(f"找不到 agent_config {run.agent_config_id}")
    agent = session.get(Agent, config.agent_id)
    if agent is None:
        raise PayloadError(f"找不到 agent {config.agent_id}")
    env = session.get(EnvironmentSpec, task_row.environment_spec_id)
    if env is None:
        raise PayloadError(f"找不到 environment_spec {task_row.environment_spec_id}")

    # 题目的完整定义存在 raw_definition 这个 JSONB 里。从它还原成 TaskDefinition，
    # 而不是从那十几个列拼——列只是给 SQL 查询用的投影，test_patch / gold_patch
    # 这些根本不在列里。
    if not task_row.raw_definition:
        raise PayloadError(
            f"题目 {task_row.task_id} 的 raw_definition 是空的，跑不了。"
            "题目入库时必须把完整定义写进去（E1-T3）"
        )
    task = TaskDefinition.model_validate(task_row.raw_definition)

    from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE

    return _Loaded(
        task=task,
        adapter_class=agent.adapter_class,
        agent_params=dict(config.params),
        model_name=config.model_name,
        # E2-T3 之前 image_tag 还是空的，退回 Golden 那个临时镜像。
        # 协议 C-36 要求正式实验引用 digest 而不是 tag，那一步在 E5-T4 的 manifest 里做。
        image=env.image_tag or DEFAULT_GOLDEN_IMAGE,
        extra_protected_paths=tuple(env.extra_protected_paths or ()),
        agent_timeout_s=task_row.agent_timeout_s,
        repo_name=task.repo_name,
    )


def _agent_config(ctx: JobContext, loaded: _Loaded) -> AgentRunnerConfig:
    """拼适配器的 harness 侧配置：Agent 镜像、密钥、追加参数。

    密钥必须过一遍 `build_env()` 的白名单。白名单挡的是"多传了一个不该进去的变量"——
    比如 `GITHUB_TOKEN`，那等于把翻原始 PR 的钥匙交给了被测 AI。名字不在名单里
    直接抛错，不会悄悄丢掉。

    哨兵适配器（Oracle / Noop / Mock）的 `model_name` 是 `none`，
    `agent_env_for()` 一把 Key 都挑不出来，所以这段对它们等价于什么都没做。
    """
    params = loaded.agent_params
    image = params.get("image")
    extra_args = params.get("extra_args") or ()
    return AgentRunnerConfig(
        image=str(image) if image else None,
        env=build_env(ctx.settings.agent_env_for(loaded.model_name)),
        extra_args=tuple(str(arg) for arg in extra_args),
    )


def _build_runner(loaded: _Loaded, payload: EvalTaskPayload, *, store: ArtifactStore) -> Any:
    """造出这次要用的适配器。

    走重放路径时**不看** `adapter_class`：这次根本不调 AI，用哪个适配器无所谓，
    交出上一次那份补丁就行（协议 C-54）。

    制品读不出来就抛异常，让整条作业失败重排，一条 attempt 记录都不写。
    不能把它变成一次"跑出 AGENT_RUNTIME_ERROR 的 attempt"——制品丢了是平台的锅，
    记在被测 AI 头上会让它的解决率无端变低。
    """
    if payload.reuse_patch_key is not None:
        patch_text = store.get(payload.reuse_patch_key).decode("utf-8")
        logger.info(
            "replaying_stored_patch",
            task_id=loaded.task.task_id,
            attempt_no=payload.attempt_no,
            key=payload.reuse_patch_key,
        )
        return StoredPatchRunner({loaded.task.task_id: patch_text})

    module_path, _, class_name = loaded.adapter_class.rpartition(".")
    if not module_path:
        raise PayloadError(f"adapter_class 不是合法的导入路径：{loaded.adapter_class!r}")
    runner_class = getattr(import_module(module_path), class_name)

    # 哨兵适配器要额外喂东西，各自的构造参数不一样。用一张显式的表而不是
    # 一串 isinstance 判断：以后接真实适配器时，在这里加一行就行。
    if class_name == "OracleRunner":
        return runner_class({loaded.task.task_id: loaded.task.gold_patch})
    if class_name == "MockRunner":
        return runner_class.from_params(
            loaded.agent_params, patches={loaded.task.task_id: loaded.task.gold_patch}
        )
    return runner_class()


# ── 事务 2 ──────────────────────────────────────────────────


def _persist_and_schedule(
    session: Session,
    ctx: JobContext,
    payload: EvalTaskPayload,
    outcome: TaskRunOutcome,
) -> None:
    """落库、决定重试、打 canonical 标记。全在调用方开的那一个事务里。"""
    # queued_at 取作业进队列的时刻，不是现在 —— 它要回答的是"这道题排了多久队"，
    # 用当前时间的话这个字段永远等于 completed_at，等于没记。
    job = session.get(JobQueue, ctx.job_id)
    task_run = EvaluationTaskRun(
        evaluation_run_id=payload.evaluation_run_id,
        benchmark_task_id=payload.benchmark_task_id,
        attempt_no=payload.attempt_no,
        lifecycle_status=LifecycleStatus.QUEUED,
        retry_of_id=payload.retry_of_id,
        worker_id=ctx.worker_id,
        queued_at=job.created_at if job is not None else None,
    )
    session.add(task_run)
    persist_task_run(session, task_run, outcome)

    history = _attempt_history(session, payload)
    decision = decide_next(history)
    logger.info(
        "retry_decision",
        task_run_id=task_run.id,
        attempt_no=payload.attempt_no,
        attempts=len(history),
        should_retry=decision.should_retry,
        canonical_attempt_no=decision.canonical_attempt_no,
        stop_reason=str(decision.stop_reason) if decision.stop_reason else None,
    )

    if decision.should_retry:
        assert decision.next_attempt_no is not None  # decide_next 保证的
        _enqueue_retry(session, ctx, payload, outcome, task_run, decision.next_attempt_no)
        return

    assert decision.canonical_attempt_no is not None
    _mark_canonical(session, payload, decision.canonical_attempt_no)


def _attempt_history(session: Session, payload: EvalTaskPayload) -> list[AttemptRecord]:
    """这道题在这次实验里的全部 attempt，按编号排序。

    刚 `persist_task_run` 写进去的那条也在里面（同一个事务里读得到）。
    只取已经跑出结论的（`infra_outcome` 非空）——非终态的记录还没有故障类型，
    参与不了 C-24 的判断。
    """
    rows = session.execute(
        sa.select(EvaluationTaskRun.attempt_no, EvaluationTaskRun.infra_outcome)
        .where(
            EvaluationTaskRun.evaluation_run_id == payload.evaluation_run_id,
            EvaluationTaskRun.benchmark_task_id == payload.benchmark_task_id,
            EvaluationTaskRun.infra_outcome.is_not(None),
        )
        .order_by(EvaluationTaskRun.attempt_no.asc())
    ).all()
    return [AttemptRecord(attempt_no=no, infra_outcome=outcome) for no, outcome in rows]


def _enqueue_retry(
    session: Session,
    ctx: JobContext,
    payload: EvalTaskPayload,
    outcome: TaskRunOutcome,
    task_run: EvaluationTaskRun,
    next_attempt_no: int,
) -> None:
    """投下一次 attempt 的作业。

    补丁已经拿到了就把它的制品 key 带上，下一次走重放（协议 C-54），不再调 AI。
    带的是**标准化**补丁：判定链后面用的就是它，原始补丁只是证据。
    """
    normalized = outcome.patches.get(PatchKind.AGENT_NORMALIZED)
    reuse_key = normalized.key if normalized is not None else None
    next_payload = EvalTaskPayload(
        evaluation_run_id=payload.evaluation_run_id,
        benchmark_task_id=payload.benchmark_task_id,
        attempt_no=next_attempt_no,
        retry_of_id=task_run.id,
        reuse_patch_key=reuse_key,
    )
    # 退避按已经跑过的 attempt 次数来。故障多半是"环境抖了一下"，
    # 立刻重排大概率再挂一次，还把机器占着。
    delay = queue.backoff_seconds(
        payload.attempt_no,
        ctx.settings.job_retry_backoff_base_s,
        cap_s=ctx.settings.job_retry_backoff_cap_s,
    )
    queue.enqueue(
        session,
        job_type=JobType.EVAL_TASK,
        payload=next_payload.to_payload(),
        # 重试优先于新题：一道题拖着不结束，整次实验就结束不了
        priority=1,
        max_attempts=ctx.settings.job_max_attempts,
        delay_s=delay,
    )
    logger.info(
        "retry_enqueued",
        task_run_id=task_run.id,
        next_attempt_no=next_attempt_no,
        infra_outcome=outcome.infra_outcome.value,
        reuse_patch=reuse_key is not None,
        delay_s=round(delay, 1),
    )


def _mark_canonical(session: Session, payload: EvalTaskPayload, attempt_no: int) -> None:
    """给认定结果那条打标（协议 C-24、C-57）。

    **不一定是刚跑完的这条。** C-58 的反例：第 1 次就 `AGENT_TIMEOUT`（不可重试），
    那它就是认定结果，哪怕后面又产生了记录。所以这里按 `attempt_no` 精确定位。
    """
    existing = session.execute(
        sa.select(EvaluationTaskRun.attempt_no).where(
            EvaluationTaskRun.evaluation_run_id == payload.evaluation_run_id,
            EvaluationTaskRun.benchmark_task_id == payload.benchmark_task_id,
            EvaluationTaskRun.is_canonical.is_(True),
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing != attempt_no:
            # 别的路径已经打过标了，而且和我们算出来的不是同一条。
            # 不覆盖：覆盖等于悄悄改写历史结论，而部分唯一索引也不允许同时有两条。
            # 记 error 让人来查——这在只有 E5-T1 的情况下不该发生。
            logger.error(
                "canonical_conflict",
                evaluation_run_id=payload.evaluation_run_id,
                benchmark_task_id=payload.benchmark_task_id,
                existing_attempt_no=existing,
                computed_attempt_no=attempt_no,
            )
        return

    session.execute(
        sa.update(EvaluationTaskRun)
        .where(
            EvaluationTaskRun.evaluation_run_id == payload.evaluation_run_id,
            EvaluationTaskRun.benchmark_task_id == payload.benchmark_task_id,
            EvaluationTaskRun.attempt_no == attempt_no,
        )
        .values(is_canonical=True)
        .execution_options(synchronize_session=False)
    )


def enqueue_eval_task(
    session: Session,
    *,
    evaluation_run_id: int,
    benchmark_task_id: int,
    priority: int = 0,
    max_attempts: int = 3,
) -> JobQueue:
    """投一道题的第一次 attempt。给编排层（E5-T2）和 CLI 用。**不 commit**。"""
    payload = EvalTaskPayload(
        evaluation_run_id=evaluation_run_id, benchmark_task_id=benchmark_task_id, attempt_no=1
    )
    return queue.enqueue(
        session,
        job_type=JobType.EVAL_TASK,
        payload=payload.to_payload(),
        priority=priority,
        max_attempts=max_attempts,
    )


__all__ = [
    "EvalTaskPayload",
    "PayloadError",
    "enqueue_eval_task",
    "handle_eval_task",
    "run_key_for",
]
