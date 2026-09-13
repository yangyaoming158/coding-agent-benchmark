"""实验的接口：建、看、取消、补跑、看子任务（E7-T0）。

## 三个写操作一行业务逻辑都不写（AC-2）

**`POST /api/runs` → `collect_provenance()` + `create_runs()`**
协议 C-27 的唯一强制点就在 `collect_provenance()` 里。路由自己拼一个
`EvaluationRun` 就能建出没有凭证、没有 manifest 的实验 —— 那种实验事后
说不清跑的是哪版代码、哪个镜像。

**`POST /api/runs/{id}/cancel` → `cancel_run()`**
取消要同时掐掉 `job_queue` 里还没被领走的作业，并把它们标成 DEAD 而不是删掉
（事后要能回答"取消时还剩多少题"）。少一步就留下幽灵作业。

**`POST /api/runs/{id}/retry-failed` → `retry_failed()`**
补跑只补窟窿，三条边界都在那个函数里：已有认定结果的题不碰（C-25）、
到了 4 次上限的不补（C-71）、上次拿到过补丁的带补丁重投由
`StoredPatchRunner` 重放、不再调 AI（C-54）。

路由这一层只做两件事：把请求参数转成函数参数，把返回的 dataclass 拼成 JSON。

## 为什么没有 `allow_dirty`

CLI 有 `--allow-dirty`（协议 C-28 的调试口子），这里**故意不给**。

理由是这两条路的使用场景不一样：命令行前面坐着一个知道自己在调试的人，
而 HTTP 端点是给网页按钮用的。一个网页按钮能悄悄建出"不得进排行榜"的实验，
过两天没人记得那次实验为什么带着 dirty 标记。

工作区不干净时这个端点返回 409 `WORKSPACE_DIRTY`，消息里写清楚该怎么办。
真要在脏工作区里跑，走命令行。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import (
    AdminTokenDep,
    Page,
    PageDep,
    SessionDep,
    get_provenance_collector,
    page_of,
)
from app.api.errors import ERROR_RESPONSES, READ_ERROR_RESPONSES, conflict, not_found
from app.benchmark.dataset import items_of, snapshot_digest
from app.domain.enums import (
    AgentOutcome,
    EvaluationRunStatus,
    InfraOutcome,
    LifecycleStatus,
)
from app.evaluation.manifest import ProvenanceError
from app.evaluation.orchestrator import (
    OrchestrationError,
    cancel_run,
    create_runs,
    retry_failed,
)
from app.infrastructure.config import get_settings
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun

router = APIRouter(prefix="/api/runs", tags=["runs"])

#: 建实验时最多允许几轮。多轮取样是建 N 个实验（协议 C-55），
#: 一次点错建出 500 个实验的代价太大，封个上限。
MAX_ROUNDS = 10


# ── 响应模型 ────────────────────────────────────────────────


class RunSummary(BaseModel):
    """实验列表里的一行。进度条要的数都在这儿，不用再查子表。"""

    id: int
    name: str
    benchmark_set_id: int
    benchmark_set: str
    agent_config_id: int
    agent_config_label: str
    agent_name: str
    #: 枚举原样透出（AC-10）。`PARTIAL` 的中文说法是"降级"，映射是展示层的事。
    status: EvaluationRunStatus
    total_tasks: int
    completed_tasks: int
    resolved_count: int
    infra_failure_count: int
    strict_resolve_rate: Decimal | None
    effective_resolve_rate: Decimal | None
    total_cost_usd: Decimal
    total_tokens: int
    makespan_ms: int | None
    agent_concurrency: int
    sandbox_concurrency: int
    retry_count: int
    recovered_infra_failure_count: int
    #: 协议 C-28：标了 dirty 的结果不得进排行榜。
    dirty: bool
    #: 人工排除出排行榜的理由，没排除就是 None。
    leaderboard_excluded_reason: str | None
    #: 创建时写入，禁止事后修改（协议 C-67）。
    protocol_version: str
    started_at: datetime | None
    finished_at: datetime | None
    created_by: str | None
    created_at: datetime


class RunDetail(RunSummary):
    """实验详情。多一份可复现性清单。"""

    #: 镜像 digest 表、harness 的 git sha、数据集摘要、环境变量白名单、随机种子。
    manifest: dict[str, Any]


class CreateRunRequest(BaseModel):
    """建实验的请求体。字段对齐 §14.4。"""

    benchmark_set_id: int
    agent_config_id: int
    #: 不给就用配置里的默认值（`BENCH_AGENT_CONCURRENCY` 等）。
    agent_concurrency: int | None = Field(None, ge=1, le=64)
    sandbox_concurrency: int | None = Field(None, ge=1, le=32)
    #: 实验名。不给就按数据集和参赛者拼一个。
    name: str | None = None
    #: 跑几轮。**每一轮是一个独立的 `EvaluationRun`**（协议 C-55、C-57），
    #: 不是在一个实验里跑 N 遍。轮间离散度就是这几个实验解决率的差。
    rounds: int = Field(1, ge=1, le=MAX_ROUNDS)


class CreateRunResponse(BaseModel):
    """建完之后返回建了哪几个。"""

    runs: list[RunSummary]
    task_count: int


class CancelResponse(BaseModel):
    """取消的结果。"""

    evaluation_run_id: int
    #: 还没被领走、直接掐掉的作业数。
    dropped_jobs: int
    #: 已经在跑的作业数。它们由各自的 Worker 中止，不在这个请求里等。
    in_flight_jobs: int
    already_cancelled: bool


class RetryResponse(BaseModel):
    """补跑的结果。"""

    evaluation_run_id: int
    #: 补投了作业的题（`benchmark_tasks.id`）。
    requeued: list[int]
    #: attempt 数已到 C-71 上限、不再补的题。
    at_attempt_cap: list[int]
    #: 已经有认定结果、按 C-25 不碰的题数。
    already_decided: int
    #: 还有作业在排队或在跑的题数。
    still_running: int


class TaskRunSummary(BaseModel):
    """Run Detail 页那张任务网格的一格。"""

    id: int
    evaluation_run_id: int
    benchmark_task_id: int
    #: 题号，一条 SQL 带出来的（AC-7：不为每一格再查一次题目表）。
    task_id: str
    issue_title: str
    attempt_no: int
    #: 这次 attempt 是不是被选为统计依据的那一次（协议 C-24、C-57）。
    is_canonical: bool
    #: 三个字段互相独立，一律原样透出，不合并也不做中文映射（协议 C-04/C-05/C-06、AC-10）。
    lifecycle_status: LifecycleStatus
    infra_outcome: InfraOutcome | None
    agent_outcome: AgentOutcome | None
    f2p_passed: int | None
    f2p_total: int | None
    p2p_passed: int | None
    p2p_total: int | None
    agent_duration_ms: int | None
    test_duration_ms: int | None
    total_duration_ms: int | None
    cost_usd: Decimal | None
    tokens_total: int | None
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


# ── 查询 ────────────────────────────────────────────────────


def _run_columns() -> sa.Select[Any]:
    """列表和详情共用的查询：一条 SQL 把数据集和参赛者的名字带出来。"""
    return (
        sa.select(
            EvaluationRun,
            BenchmarkSet.slug,
            BenchmarkSet.version,
            AgentConfig.label,
            Agent.name,
        )
        .join(BenchmarkSet, BenchmarkSet.id == EvaluationRun.benchmark_set_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
    )


def _summary(row: sa.Row[Any]) -> RunSummary:
    run, slug, version, label, agent_name = row
    return RunSummary(
        id=run.id,
        name=run.name,
        benchmark_set_id=run.benchmark_set_id,
        benchmark_set=f"{slug}@{version}",
        agent_config_id=run.agent_config_id,
        agent_config_label=label,
        agent_name=agent_name,
        status=run.status,
        total_tasks=run.total_tasks,
        completed_tasks=run.completed_tasks,
        resolved_count=run.resolved_count,
        infra_failure_count=run.infra_failure_count,
        strict_resolve_rate=run.strict_resolve_rate,
        effective_resolve_rate=run.effective_resolve_rate,
        total_cost_usd=run.total_cost_usd,
        total_tokens=run.total_tokens,
        makespan_ms=run.makespan_ms,
        agent_concurrency=run.agent_concurrency,
        sandbox_concurrency=run.sandbox_concurrency,
        retry_count=run.retry_count,
        recovered_infra_failure_count=run.recovered_infra_failure_count,
        dirty=run.dirty,
        leaderboard_excluded_reason=run.leaderboard_excluded_reason,
        protocol_version=run.protocol_version,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_by=run.created_by,
        created_at=run.created_at,
    )


def _load_run(session: SessionDep, run_id: int) -> sa.Row[Any]:
    row = session.execute(_run_columns().where(EvaluationRun.id == run_id)).first()
    if row is None:
        raise not_found("RUN_NOT_FOUND", f"找不到实验 #{run_id}")
    return row


@router.get("", responses=READ_ERROR_RESPONSES)
def list_runs(
    session: SessionDep,
    page: PageDep,
    set_id: Annotated[int | None, Query(alias="set", description="只看这一版数据集的")] = None,
    agent_config_id: Annotated[
        int | None, Query(alias="agent_config", description="只看这个参赛者的")
    ] = None,
    run_status: Annotated[
        EvaluationRunStatus | None, Query(alias="status", description="只看这个状态的")
    ] = None,
) -> Page[RunSummary]:
    """实验列表，新的在前。进度条要的数全在行里，**一条 SQL**（AC-7）。"""
    where: list[sa.ColumnElement[bool]] = []
    if set_id is not None:
        where.append(EvaluationRun.benchmark_set_id == set_id)
    if agent_config_id is not None:
        where.append(EvaluationRun.agent_config_id == agent_config_id)
    if run_status is not None:
        where.append(EvaluationRun.status == run_status)

    total = int(
        session.execute(
            sa.select(sa.func.count()).select_from(EvaluationRun).where(*where)
        ).scalar_one()
    )
    rows = session.execute(
        _run_columns()
        .where(*where)
        .order_by(EvaluationRun.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    return page_of([_summary(row) for row in rows], total=total, params=page)


@router.get("/{run_id}", response_model=RunDetail, responses=READ_ERROR_RESPONSES)
def get_run(run_id: int, session: SessionDep) -> RunDetail:
    """一次实验的详情，带可复现性清单。"""
    row = _load_run(session, run_id)
    return RunDetail(**_summary(row).model_dump(), manifest=dict(row[0].manifest or {}))


@router.get("/{run_id}/task-runs", responses=READ_ERROR_RESPONSES)
def list_task_runs(
    run_id: int,
    session: SessionDep,
    page: PageDep,
    lifecycle_status: Annotated[
        LifecycleStatus | None, Query(alias="status", description="只看这个生命周期状态的")
    ] = None,
    canonical_only: Annotated[
        bool, Query(description="只看认定结果那一次 attempt（协议 C-24）")
    ] = False,
) -> Page[TaskRunSummary]:
    """一次实验的逐题执行记录。

    排序是 `(题目 id, attempt 号)`，两者合起来唯一，所以翻页稳定（AC-4）。

    **一条 SQL**：题号和 issue 标题是 join 出来的，不是每格再查一次
    （22 道题的网格，一不留神就是 23 条查询，AC-7 点名的就是这个）。
    """
    _load_run(session, run_id)  # 实验不存在要 404，不能返回一个空列表

    where: list[sa.ColumnElement[bool]] = [EvaluationTaskRun.evaluation_run_id == run_id]
    if lifecycle_status is not None:
        where.append(EvaluationTaskRun.lifecycle_status == lifecycle_status)
    if canonical_only:
        where.append(EvaluationTaskRun.is_canonical.is_(True))

    total = int(
        session.execute(
            sa.select(sa.func.count()).select_from(EvaluationTaskRun).where(*where)
        ).scalar_one()
    )
    rows = session.execute(
        sa.select(EvaluationTaskRun, BenchmarkTask.task_id, BenchmarkTask.issue_title)
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .where(*where)
        .order_by(EvaluationTaskRun.benchmark_task_id, EvaluationTaskRun.attempt_no)
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    return page_of(
        [_task_run_summary(task_run, task_id, title) for task_run, task_id, title in rows],
        total=total,
        params=page,
    )


def _task_run_summary(run: EvaluationTaskRun, task_id: str, issue_title: str) -> TaskRunSummary:
    return TaskRunSummary(
        id=run.id,
        evaluation_run_id=run.evaluation_run_id,
        benchmark_task_id=run.benchmark_task_id,
        task_id=task_id,
        issue_title=issue_title,
        attempt_no=run.attempt_no,
        is_canonical=run.is_canonical,
        lifecycle_status=run.lifecycle_status,
        infra_outcome=run.infra_outcome,
        agent_outcome=run.agent_outcome,
        f2p_passed=run.f2p_passed,
        f2p_total=run.f2p_total,
        p2p_passed=run.p2p_passed,
        p2p_total=run.p2p_total,
        agent_duration_ms=run.agent_duration_ms,
        test_duration_ms=run.test_duration_ms,
        total_duration_ms=run.total_duration_ms,
        cost_usd=run.cost_usd,
        tokens_total=run.tokens_total,
        error_code=run.error_code,
        started_at=run.prepare_started_at,
        completed_at=run.completed_at,
    )


# ── 写操作 ──────────────────────────────────────────────────


@router.post(
    "",
    response_model=CreateRunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AdminTokenDep],
    responses=ERROR_RESPONSES,
)
def create_run(
    body: CreateRunRequest,
    session: SessionDep,
    collector: Annotated[Any, Depends(get_provenance_collector)],
) -> CreateRunResponse:
    """建一次（或多轮）实验，并把题展开成作业。

    题从**数据集快照**（`benchmark_set_items`）里取，不是整张 `benchmark_tasks` ——
    那张表里混着 INVALID 的题和别的数据集的题，全投进去解决率的分母就不对了。
    """
    settings = get_settings()
    dataset = session.get(BenchmarkSet, body.benchmark_set_id)
    if dataset is None:
        raise not_found("BENCHMARK_SET_NOT_FOUND", f"找不到数据集版本 #{body.benchmark_set_id}")
    config = session.execute(
        sa.select(AgentConfig, Agent.name)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .where(AgentConfig.id == body.agent_config_id)
    ).first()
    if config is None:
        raise not_found("AGENT_CONFIG_NOT_FOUND", f"找不到参赛者 #{body.agent_config_id}")

    rows = items_of(session, dataset.id)
    if not rows:
        raise conflict(
            "BENCHMARK_SET_EMPTY",
            f"{dataset.slug}@{dataset.version} 里一道题都没有，先跑 `make dataset-stage`",
        )
    task_ids = [row.benchmark_task_id for row in rows]

    try:
        provenance = collector(
            session,
            benchmark_set_id=dataset.id,
            snapshot_digest=snapshot_digest(rows),
            agent_config_id=body.agent_config_id,
            task_ids=task_ids,
            agent_concurrency=body.agent_concurrency or settings.agent_concurrency,
            sandbox_concurrency=body.sandbox_concurrency or settings.sandbox_concurrency,
            job_max_attempts=settings.job_max_attempts,
        )
        runs = create_runs(
            session,
            name=body.name or f"{dataset.slug}@{dataset.version} × {config[0].label}",
            task_ids=task_ids,
            provenance=provenance,
            rounds=body.rounds,
            created_by="api",
        )
    except ProvenanceError as exc:
        # 绝大多数情况是协议 C-27：工作区有未提交改动。这不是参数写错了，
        # 是当前状态下做不了这件事，所以是 409 不是 422
        raise conflict("WORKSPACE_DIRTY", str(exc)) from exc
    except OrchestrationError as exc:
        raise conflict("CANNOT_CREATE_RUN", str(exc)) from exc

    session.commit()
    created = [
        _summary(row)
        for row in session.execute(
            _run_columns()
            .where(EvaluationRun.id.in_([r.id for r in runs]))
            .order_by(EvaluationRun.id)
        ).all()
    ]
    return CreateRunResponse(runs=created, task_count=len(task_ids))


@router.post(
    "/{run_id}/cancel",
    response_model=CancelResponse,
    dependencies=[AdminTokenDep],
    responses=ERROR_RESPONSES,
)
def cancel(run_id: int, session: SessionDep) -> CancelResponse:
    """取消一次实验。转给 `cancel_run()`，这里一行业务逻辑都没有。"""
    _load_run(session, run_id)
    try:
        summary = cancel_run(session, run_id)
    except OrchestrationError as exc:
        # 已经跑完的实验没什么可取消的
        raise conflict("RUN_ALREADY_FINISHED", str(exc)) from exc
    session.commit()
    return CancelResponse(
        evaluation_run_id=summary.evaluation_run_id,
        dropped_jobs=summary.dropped_jobs,
        in_flight_jobs=summary.in_flight_jobs,
        already_cancelled=summary.already_cancelled,
    )


@router.post(
    "/{run_id}/retry-failed",
    response_model=RetryResponse,
    dependencies=[AdminTokenDep],
    responses=ERROR_RESPONSES,
)
def retry(run_id: int, session: SessionDep) -> RetryResponse:
    """把没有认定结果、也没有在跑的题补投一次作业。转给 `retry_failed()`。"""
    _load_run(session, run_id)
    try:
        summary = retry_failed(session, run_id)
    except OrchestrationError as exc:
        # COMPLETED 的实验每道题都有结论，没有洞可补；要再跑一遍得新建实验（C-55）
        raise conflict("RUN_ALREADY_COMPLETED", str(exc)) from exc
    session.commit()
    return RetryResponse(
        evaluation_run_id=summary.evaluation_run_id,
        requeued=list(summary.requeued),
        at_attempt_cap=list(summary.at_attempt_cap),
        already_decided=summary.already_decided,
        still_running=summary.still_running,
    )


__all__ = [
    "MAX_ROUNDS",
    "CancelResponse",
    "CreateRunRequest",
    "CreateRunResponse",
    "RetryResponse",
    "RunDetail",
    "RunSummary",
    "TaskRunSummary",
    "router",
]
