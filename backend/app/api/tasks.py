"""题目的接口：列表和详情（E7-T0）。

对着 §16.2 的 Benchmark Detail 页写的 —— 那一页的任务表格要按
**仓库 / 难度 / 语言 / 状态**四个维度筛，所以 §14.4 写的
`?set=&state=&q=` 三个参数不够，这里补到六个。

## 两个字段故意不透出

`gold_patch_uri`（官方修复补丁）和 `test_patch_paths`（官方测试补丁改了哪些文件）
一个都不返回。

协议 C-44 禁止把 gold patch 发给被测 AI，C-76 禁止下发 `test_patch_paths` ——
那等于直接告诉它官方测试改了哪几个文件。这两条管的是**发给 AI 的任务输入**，
而读接口是开放的（§14.4：写操作要 token，读接口开放），任何人都能拉。
把它们放进一个开放的 JSON 接口，等于给绕过任务输入开了第二扇门。

前端也没有一个 P0 页面要这两个字段。要看官方补丁走命令行
（`python -m cli.task show`），那条路上有人在场。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES, not_found
from app.domain.enums import IssueLanguage, TaskDifficulty, TaskValidationState
from app.infrastructure.models.benchmark import (
    BenchmarkSetItem,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskSummary(BaseModel):
    """列表里的一行。"""

    id: int
    #: 人可读的稳定标识，如 `pallets__click-2721`。详情端点按它取。
    task_id: str
    repository: str
    environment_id: str
    base_commit: str
    issue_title: str
    #: 三个枚举都原样透出，不做中文映射（AC-10）。
    issue_language: IssueLanguage
    difficulty: TaskDifficulty
    validation_state: TaskValidationState
    tags: list[str]
    #: F2P / P2P 只给条数，清单在详情里。列表一行放几十个用例 ID 没人看得过来。
    fail_to_pass_count: int
    pass_to_pass_count: int
    created_at: datetime


class TaskDetail(TaskSummary):
    """详情。多的是 issue 正文、用例清单、资源限额和验证结论。"""

    issue_body: str
    source_issue_url: str | None
    source_pr_url: str | None
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    agent_timeout_s: int
    test_timeout_s: int
    sandbox_cpu: Decimal
    sandbox_memory_mb: int
    sandbox_pids_limit: int
    invalid_reason_code: str | None
    validated_at: datetime | None
    validation_evidence_uri: str | None
    #: 这道题为什么被隔离：`{at, from_state, reason}`。没隔离过就是 None。
    quarantine: dict[str, Any] | None
    content_hash: str


#: 列表和详情共用的取数：一条 SQL 把仓库名和环境 ID 带出来。
#: 不带的话前端拿到的是两个外键 id，得再发 N 次请求去换名字（AC-7 的 N+1）。
_BASE_COLUMNS = (
    BenchmarkTask.id,
    BenchmarkTask.task_id,
    Repository.full_name,
    EnvironmentSpec.environment_id,
    BenchmarkTask.base_commit,
    BenchmarkTask.issue_title,
    BenchmarkTask.issue_language,
    BenchmarkTask.difficulty,
    BenchmarkTask.validation_state,
    BenchmarkTask.tags,
    sa.func.jsonb_array_length(BenchmarkTask.fail_to_pass),
    sa.func.jsonb_array_length(BenchmarkTask.pass_to_pass),
    BenchmarkTask.created_at,
)


def _summary_from(row: sa.Row[Any]) -> TaskSummary:
    return TaskSummary(
        id=row[0],
        task_id=row[1],
        repository=row[2],
        environment_id=row[3],
        base_commit=row[4],
        issue_title=row[5],
        issue_language=row[6],
        difficulty=row[7],
        validation_state=row[8],
        tags=list(row[9] or []),
        fail_to_pass_count=int(row[10]),
        pass_to_pass_count=int(row[11]),
        created_at=row[12],
    )


def _joined() -> sa.Select[Any]:
    return (
        sa.select(*_BASE_COLUMNS)
        .join(Repository, Repository.id == BenchmarkTask.repository_id)
        .join(EnvironmentSpec, EnvironmentSpec.id == BenchmarkTask.environment_spec_id)
    )


@router.get("", responses=READ_ERROR_RESPONSES)
def list_tasks(
    session: SessionDep,
    page: PageDep,
    set_id: Annotated[
        int | None, Query(alias="set", description="只看这一版数据集快照里冻住的题")
    ] = None,
    state: Annotated[TaskValidationState | None, Query(description="验证状态")] = None,
    repo: Annotated[str | None, Query(description="仓库全名，如 pallets/click")] = None,
    difficulty: Annotated[TaskDifficulty | None, Query(description="难度")] = None,
    language: Annotated[IssueLanguage | None, Query(description="issue 语言")] = None,
    q: Annotated[str | None, Query(description="在题号和 issue 标题里搜，不区分大小写")] = None,
) -> Page[TaskSummary]:
    """题目列表。六个筛选条件对应 Benchmark Detail 页的四个筛选器加搜索框。"""
    where: list[sa.ColumnElement[bool]] = []
    if state is not None:
        where.append(BenchmarkTask.validation_state == state)
    if repo is not None:
        where.append(Repository.full_name == repo)
    if difficulty is not None:
        where.append(BenchmarkTask.difficulty == difficulty)
    if language is not None:
        where.append(BenchmarkTask.issue_language == language)
    if q:
        pattern = f"%{q}%"
        where.append(
            sa.or_(BenchmarkTask.task_id.ilike(pattern), BenchmarkTask.issue_title.ilike(pattern))
        )
    if set_id is not None:
        # 用 EXISTS 而不是 join：一道题在一个 set 里最多一行，join 不会放大结果，
        # 但 EXISTS 表达的意思更准 —— 我们要的是"在不在这一版里"，不是"取出关联行"
        where.append(
            sa.exists().where(
                BenchmarkSetItem.benchmark_set_id == set_id,
                BenchmarkSetItem.benchmark_task_id == BenchmarkTask.id,
            )
        )

    total = int(
        session.execute(
            sa.select(sa.func.count())
            .select_from(BenchmarkTask)
            .join(Repository, Repository.id == BenchmarkTask.repository_id)
            .where(*where)
        ).scalar_one()
    )
    rows = session.execute(
        _joined().where(*where).order_by(BenchmarkTask.id).limit(page.limit).offset(page.offset)
    ).all()
    return page_of([_summary_from(row) for row in rows], total=total, params=page)


@router.get("/{task_id}", response_model=TaskDetail, responses=READ_ERROR_RESPONSES)
def get_task(task_id: str, session: SessionDep) -> TaskDetail:
    """一道题的详情。按 `task_id`（字符串题号）取，不是主键。

    §14.4 写的就是 `/api/tasks/{task_id}`，而题号才是人会从别处抄过来的那个 ——
    日志、验证证据、数据集 CSV 里出现的都是 `pallets__click-2721`。
    """
    row = session.execute(_joined().where(BenchmarkTask.task_id == task_id)).first()
    if row is None:
        raise not_found("TASK_NOT_FOUND", f"找不到题目 {task_id}")
    task = session.execute(
        sa.select(BenchmarkTask).where(BenchmarkTask.task_id == task_id)
    ).scalar_one()

    return TaskDetail(
        **_summary_from(row).model_dump(),
        issue_body=task.issue_body,
        source_issue_url=task.source_issue_url,
        source_pr_url=task.source_pr_url,
        fail_to_pass=list(task.fail_to_pass),
        pass_to_pass=list(task.pass_to_pass),
        agent_timeout_s=task.agent_timeout_s,
        test_timeout_s=task.test_timeout_s,
        sandbox_cpu=task.sandbox_cpu,
        sandbox_memory_mb=task.sandbox_memory_mb,
        sandbox_pids_limit=task.sandbox_pids_limit,
        invalid_reason_code=task.invalid_reason_code,
        validated_at=task.validated_at,
        validation_evidence_uri=task.validation_evidence_uri,
        quarantine=task.quarantine,
        content_hash=task.content_hash,
    )


__all__ = ["TaskDetail", "TaskSummary", "router"]
