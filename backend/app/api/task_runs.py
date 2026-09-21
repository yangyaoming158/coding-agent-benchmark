"""单题单次执行的接口：详情、逐条用例、制品（E7-T0）。

§16.2 的 Task Run Detail 页是整个平台的核心用户旅程 ——
"任何页面在 3 次点击内能到达某个 Agent 在某道题上为什么失败"（§16.3）。
这三个端点就是那一页的全部数据源。归因结果（E6 的 `failure_attributions`）
2026-09-21 起也从详情里带出来，见下面"归因随详情走，但盲检期间藏起来"。

## 制品不塞进 JSON（AC-6）

一次评测的 Agent stdout 能到几百 MB。塞进 JSON 会把前端和后端内存一起打挂，
而且 JSON 里的超长字符串没法边下边看。

所以 `/artifacts/{kind}` 走两条路，由存储后端自己说了算：

- `store.url()` 给得出签名 URL（MinIO）→ **302** 过去，让浏览器直连，
  字节根本不经过后端；
- 给不出（本地存储永远返回 None）→ **流式转发**，一块一块读一块一块写，
  内存里同时只有一个块。

判断依据是 `url()` 的返回值，不是配置项 —— 换后端时这段代码不用改
（ADR-005 那句"业务代码零改动"）。

## 补丁正文也走这个端点，但它在另一张表里

`{kind}` 接受**两套枚举**：`ArtifactKind`（日志、轨迹、测试报告）和
`AgentPatchKind`（`AGENT_RAW` / `AGENT_NORMALIZED`）。
后者是 `PatchKind` 四个值里的两个 —— 另外两个是官方补丁，见那个类的说明。

不是为了好看，是因为文件索引确实分在两张表：日志类在 `artifacts`，
补丁在 `patch_artifacts`（那张表多出 `files_changed` / `is_empty` /
`applies_cleanly` 这些补丁独有的统计，所以当初没并进 `artifacts`）。
`ArtifactKind` 里那个 `PATCH` 值**全库没有任何一处往里写** ——
2026-09-12 查过，`artifacts` 表里 7 种 kind 没有 PATCH。

对调用方来说这个分表没有意义：它只想问"给我这次执行的某个文件"。
所以端点按名字去两张表里找，找不到才 404。不这么做的话，
§16.2 的 Task Run Detail 页那个 **Patch Viewer 根本取不到 diff 正文** ——
详情接口只给补丁的统计（AC-6：正文不进 JSON），正文只能从这里拿。

## 归因随详情走，但盲检期间藏起来

`failure_attributions` 上 `evaluation_task_run_id` 是唯一约束，一次执行最多一条结论，
所以详情里是单数的 `failure_attribution`，没有就是 None（修好了的、规则分不出又没跑
LLM 的都是 None，前端按 `agent_outcome` 区分这两种"没有"）。`raw_response`（低置信三票
的原始回答）不透出：大而没人看，页面要的是类别、置信度、证据和中文理由。

**这是个不要 token 的开放读接口**，而 E6-T3 的盲检要求标注者提交前看不到机器答案
（AC 4："不能只靠前端隐藏"）。盲检队列里就写着 task_run_id，在地址栏敲一下单题页
就能看到。所以配置 `BENCH_BLIND_REVIEW=true` 时，这里把 `failure_attribution` 置空、
并标 `attribution_withheld=true` —— 字段在 JSON 里根本不存在内容，不是前端藏起来的。
抽检那几天打开，标完关掉，改完重启 api。`/api/review/*` 那三个端点不受它影响，
它们自己有"提交前不返回"的规则。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import IO, Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES, not_found
from app.domain.enums import (
    AgentOutcome,
    ArtifactKind,
    ArtifactOwnerType,
    AttributionStage,
    AttributionStatus,
    CostSource,
    FailureCategory,
    InfraOutcome,
    LifecycleStatus,
    PatchKind,
    TestRole,
    TestStatus,
)
from app.infrastructure.config import get_settings
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import FailureAttribution
from app.infrastructure.models.benchmark import BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationTaskRun, PatchArtifact, TestResult
from app.storage import ArtifactNotFoundError, create_artifact_store, key_from_uri


class AgentPatchKind(StrEnum):
    """这个端点**允许**取的补丁种类：只有被测 AI 自己交出来的那两份。

    `app.domain.enums.PatchKind` 一共四个值，另外两个**绝不能从这里出去**：

    - `GOLD` 是官方修复补丁。协议 C-44 禁止它到达被测 AI ——
      而这是个不要 token 的开放读接口，谁都能拉。
    - `TEST` 是官方测试补丁，C-76 是同一个道理。

    做成一个独立的窄枚举、而不是在函数里加一句 `if kind is GOLD: raise`：
    这样限制会进 OpenAPI，`make gen-api` 生成的前端类型里**根本没有 GOLD 这个选项**，
    写错了当场编译不过。运行时检查会被忘掉，类型不会。

    库里现在只有这两种（2026-09-12 查过 `patch_artifacts`：431 + 431），
    但枚举允许四种 —— 挡在这里，而不是指望将来没人往里写。
    """

    AGENT_RAW = PatchKind.AGENT_RAW.value
    AGENT_NORMALIZED = PatchKind.AGENT_NORMALIZED.value


router = APIRouter(prefix="/api/task-runs", tags=["task-runs"])

#: 流式转发时每次读多少。和 `LocalArtifactStore` 内部用的块大小一致。
_CHUNK_SIZE = 1024 * 1024


class PatchSummary(BaseModel):
    """一份补丁的统计。**只给统计，不给正文**。

    正文走 `/artifacts/{kind}`，`kind` 填这里的 `AGENT_RAW` 或 `AGENT_NORMALIZED`。
    """

    #: `AGENT_RAW` 是 AI 交出来的原样，`AGENT_NORMALIZED` 是过滤受保护路径之后的。
    kind: PatchKind
    sha256: str
    size_bytes: int
    files_changed: int
    lines_added: int
    lines_deleted: int
    is_empty: bool
    applies_cleanly: bool | None


class ArtifactSummary(BaseModel):
    """一个制品的索引。前端靠 `kind` 拼下载链接。"""

    kind: ArtifactKind
    content_type: str
    #: 原始内容的字节数（不是压缩后占的磁盘）。
    size_bytes: int
    sha256: str
    created_at: datetime


class FailureAttributionSummary(BaseModel):
    """这次失败"为什么没修好"的自动结论（`failure_attributions` 一行）。

    协议 C-40：它只解释原因，**不回写判定**。`agent_outcome` 说的是"修没修好"，
    这里说的是"没修好是哪一类"，两者独立。
    """

    #: 哪一层给的结论：RULE 规则、LLM 大模型、HUMAN 人工。枚举原样透出（AC-10）。
    stage: AttributionStage
    #: F1～F8 是 AI 的问题，N1 平台故障，N2 题目缺陷（`06-judge-attribution.md` §12.1）。
    category: FailureCategory
    #: 候选解释，只有 LLM 层会给。
    secondary_category: FailureCategory | None
    #: 0～1。规则层没有置信度（它是确定性判定），这里是 None，**不是 0**。
    confidence: Decimal | None
    #: OK 可信；NEEDS_HUMAN 自动归因给不出可信结论；FAILED 归因本身失败。
    status: AttributionStatus
    judge_model: str | None
    #: 提示词指纹。换了提示词，历史结果就不再可比，靠它能查出来。
    prompt_hash: str | None
    #: 支撑结论的证据。规则层是 `{rule, facts}`，
    #: LLM 层是 `{citations: [{source, quote}], vote_categories}`。
    #: 形状随层级不同，原样透出，翻译是展示层的事。
    evidence: dict[str, Any]
    reasoning_zh: str | None
    created_at: datetime


class TaskRunDetail(BaseModel):
    """一次执行的完整经过。"""

    id: int
    evaluation_run_id: int
    benchmark_task_id: int
    task_id: str
    issue_title: str
    attempt_no: int
    #: 协议 C-24、C-57：这次是不是被选为统计依据的那一次。
    is_canonical: bool
    #: 这次是哪一次 attempt 的重试。没有就是 None。
    retry_of_id: int | None

    #: 三个字段互相独立（协议 C-03/C-04/C-05/C-06），原样透出，
    #: **不合并也不做中文映射**（AC-10）。合并了解决率就不可信。
    lifecycle_status: LifecycleStatus
    infra_outcome: InfraOutcome | None
    agent_outcome: AgentOutcome | None

    queued_at: datetime | None
    prepare_started_at: datetime | None
    #: 协议 C-77：Agent 容器成功启动、且任务输入已写入其标准输入的那一刻。
    #: 整张合法组合表靠它区分"没给 AI 机会"和"给了机会但没拿到结论"。
    agent_started_at: datetime | None
    agent_finished_at: datetime | None
    test_started_at: datetime | None
    test_finished_at: datetime | None
    judged_at: datetime | None
    completed_at: datetime | None

    agent_duration_ms: int | None
    test_duration_ms: int | None
    total_duration_ms: int | None
    exit_code: int | None

    tokens_input: int | None
    tokens_output: int | None
    #: 提示缓存命中的 token 数。**是 `tokens_input` 的一部分，不另加**。
    tokens_cache_read: int | None
    tokens_total: int | None
    cost_usd: Decimal | None
    #: 协议纪律 3 要求 reported / estimated / unavailable 区分显示。
    #: `unavailable` 时上面那个金额是缺的，不是 0。
    cost_source: CostSource | None
    turns: int | None

    files_changed: int | None
    lines_added: int | None
    lines_deleted: int | None
    f2p_passed: int | None
    f2p_total: int | None
    p2p_passed: int | None
    p2p_total: int | None

    error_code: str | None
    error_message_excerpt: str | None
    worker_id: str | None

    #: 下面三个是诊断字段（协议 C-08b）：`EMPTY_PATCH` 有歧义，
    #: "AI 什么都没做"和"改的全是受保护文件被丢光了"结果都是空补丁。
    raw_patch_empty: bool | None
    #: 为 true 时本身就要触发人工复核（协议 C-13d）。
    protected_path_edit_attempted: bool | None
    filtered_change_reasons: list[dict[str, Any]] | None

    patches: list[PatchSummary]
    artifacts: list[ArtifactSummary]

    #: 自动归因。None 有两种意思：修好了没有失败，或者失败了但还没有结论
    #: （规则分不出、LLM 没跑）；前端按 `agent_outcome` 区分。
    failure_attribution: FailureAttributionSummary | None
    #: 为 true 时上面那个 None 是**被藏起来的**（配置 `BENCH_BLIND_REVIEW`，盲检期间），
    #: 不代表没有结论。见模块开头最后一节。
    attribution_withheld: bool


class TestResultRow(BaseModel):
    """一条用例的结果 —— 判定的证据。"""

    id: int
    #: 归一化之后的用例 ID（协议 C-13a）。
    test_id: str
    #: F2P / P2P，原样透出。
    role: TestRole
    status: TestStatus
    duration_ms: int | None
    message_excerpt: str | None


def _load(session: SessionDep, task_run_id: int) -> tuple[EvaluationTaskRun, str, str]:
    row = session.execute(
        sa.select(EvaluationTaskRun, BenchmarkTask.task_id, BenchmarkTask.issue_title)
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .where(EvaluationTaskRun.id == task_run_id)
    ).first()
    if row is None:
        raise not_found("TASK_RUN_NOT_FOUND", f"找不到执行记录 #{task_run_id}")
    return row[0], row[1], row[2]


@router.get("/{task_run_id}", response_model=TaskRunDetail, responses=READ_ERROR_RESPONSES)
def get_task_run(task_run_id: int, session: SessionDep) -> TaskRunDetail:
    """一次执行的详情，带补丁统计、制品清单和自动归因。

    四条 SQL（本体 + 补丁 + 制品 + 归因），**条数不随制品数量增长**（AC-7）。
    盲检开关开着时归因那条不发，直接置空。
    """
    task_run, task_id, issue_title = _load(session, task_run_id)
    withheld = get_settings().blind_review
    attribution = (
        None
        if withheld
        else session.execute(
            sa.select(FailureAttribution).where(
                FailureAttribution.evaluation_task_run_id == task_run_id
            )
        ).scalar_one_or_none()
    )
    patches = (
        session.execute(
            sa.select(PatchArtifact)
            .where(PatchArtifact.evaluation_task_run_id == task_run_id)
            .order_by(PatchArtifact.kind)
        )
        .scalars()
        .all()
    )
    artifacts = (
        session.execute(
            sa.select(Artifact)
            .where(
                Artifact.owner_type == ArtifactOwnerType.TASK_RUN,
                Artifact.owner_id == task_run_id,
            )
            .order_by(Artifact.kind)
        )
        .scalars()
        .all()
    )
    return TaskRunDetail(
        id=task_run.id,
        evaluation_run_id=task_run.evaluation_run_id,
        benchmark_task_id=task_run.benchmark_task_id,
        task_id=task_id,
        issue_title=issue_title,
        attempt_no=task_run.attempt_no,
        is_canonical=task_run.is_canonical,
        retry_of_id=task_run.retry_of_id,
        lifecycle_status=task_run.lifecycle_status,
        infra_outcome=task_run.infra_outcome,
        agent_outcome=task_run.agent_outcome,
        queued_at=task_run.queued_at,
        prepare_started_at=task_run.prepare_started_at,
        agent_started_at=task_run.agent_started_at,
        agent_finished_at=task_run.agent_finished_at,
        test_started_at=task_run.test_started_at,
        test_finished_at=task_run.test_finished_at,
        judged_at=task_run.judged_at,
        completed_at=task_run.completed_at,
        agent_duration_ms=task_run.agent_duration_ms,
        test_duration_ms=task_run.test_duration_ms,
        total_duration_ms=task_run.total_duration_ms,
        exit_code=task_run.exit_code,
        tokens_input=task_run.tokens_input,
        tokens_output=task_run.tokens_output,
        tokens_cache_read=task_run.tokens_cache_read,
        tokens_total=task_run.tokens_total,
        cost_usd=task_run.cost_usd,
        cost_source=task_run.cost_source,
        turns=task_run.turns,
        files_changed=task_run.files_changed,
        lines_added=task_run.lines_added,
        lines_deleted=task_run.lines_deleted,
        f2p_passed=task_run.f2p_passed,
        f2p_total=task_run.f2p_total,
        p2p_passed=task_run.p2p_passed,
        p2p_total=task_run.p2p_total,
        error_code=task_run.error_code,
        error_message_excerpt=task_run.error_message_excerpt,
        worker_id=task_run.worker_id,
        raw_patch_empty=task_run.raw_patch_empty,
        protected_path_edit_attempted=task_run.protected_path_edit_attempted,
        filtered_change_reasons=task_run.filtered_change_reasons,
        patches=[PatchSummary.model_validate(p, from_attributes=True) for p in patches],
        artifacts=[ArtifactSummary.model_validate(a, from_attributes=True) for a in artifacts],
        failure_attribution=(
            FailureAttributionSummary.model_validate(attribution, from_attributes=True)
            if attribution is not None
            else None
        ),
        attribution_withheld=withheld,
    )


@router.get("/{task_run_id}/tests", responses=READ_ERROR_RESPONSES)
def list_tests(
    task_run_id: int,
    session: SessionDep,
    page: PageDep,
    role: Annotated[TestRole | None, Query(description="只看 F2P 或 P2P")] = None,
    test_status: Annotated[
        TestStatus | None, Query(alias="status", description="只看某个状态的用例")
    ] = None,
) -> Page[TestResultRow]:
    """逐条用例的结果。

    排序是 `(role, test_id, id)`。带上 id 是因为同一个 `test_id` 在参数化用例里
    可能出现多次，只按前两项排不唯一，翻页会不稳（AC-4）。
    """
    _load(session, task_run_id)  # 不存在要 404，不能返回空列表

    where: list[sa.ColumnElement[bool]] = [TestResult.evaluation_task_run_id == task_run_id]
    if role is not None:
        where.append(TestResult.role == role)
    if test_status is not None:
        where.append(TestResult.status == test_status)

    total = int(
        session.execute(
            sa.select(sa.func.count()).select_from(TestResult).where(*where)
        ).scalar_one()
    )
    rows = (
        session.execute(
            sa.select(TestResult)
            .where(*where)
            .order_by(TestResult.role, TestResult.test_id, TestResult.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        .scalars()
        .all()
    )
    return page_of(
        [TestResultRow.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        params=page,
    )


@router.get(
    "/{task_run_id}/artifacts/{kind}",
    # 返回的是 302 或者字节流，两种都不是 Pydantic 模型。
    # 不写这一条 FastAPI 会拿返回类型标注去生成响应模型，然后在装配时就炸
    response_model=None,
    responses={
        200: {"description": "制品内容（流式）", "content": {"*/*": {}}},
        302: {"description": "重定向到签名 URL"},
        **READ_ERROR_RESPONSES,
    },
)
def get_artifact(
    task_run_id: int, kind: ArtifactKind | AgentPatchKind, session: SessionDep
) -> RedirectResponse | StreamingResponse:
    """取一份制品或一份补丁的正文。**不把内容塞进 JSON**（AC-6）。

    能拿到签名 URL 就 302 过去，拿不到就流式转发。
    `kind` 为什么接受两套枚举，见模块开头第二节。
    """
    _load(session, task_run_id)
    found = _locate_file(session, task_run_id, kind)
    if found is None:
        raise not_found("ARTIFACT_NOT_FOUND", f"执行记录 #{task_run_id} 没有 {kind.value} 这份制品")
    uri, content_type, size_bytes, sha256 = found

    store = create_artifact_store()
    key = key_from_uri(uri)
    signed = store.url(key)
    if signed is not None:
        # MinIO：让浏览器直连，几百 MB 的日志一个字节都不经过后端
        return RedirectResponse(signed, status_code=302)

    try:
        stream = store.open(key)
    except ArtifactNotFoundError as exc:
        # 库里有索引行、磁盘上文件没了。这种不一致要说出来，
        # 不能返回一个空文件 —— 空日志和"日志被清掉了"是两回事
        raise not_found(
            "ARTIFACT_FILE_MISSING",
            f"库里记着 {uri}，但制品存储里找不到这个文件",
        ) from exc

    return StreamingResponse(
        _chunks(stream),
        media_type=content_type,
        headers={
            # 原始内容的大小。压缩存的那份磁盘上更小，但下载出来是这个数
            "Content-Length": str(size_bytes),
            "Content-Disposition": f'inline; filename="{kind.value.lower()}"',
            "X-Artifact-Sha256": sha256,
        },
    )


def _locate_file(
    session: Session, task_run_id: int, kind: ArtifactKind | AgentPatchKind
) -> tuple[str, str, int, str] | None:
    """按 kind 去两张表里找这个文件，返回（uri, 内容类型, 原始字节数, sha256）。

    补丁表没有 `content_type` 列 —— 那张表只装补丁，类型是固定的。
    用 `text/x-diff` 而不是 `text/plain`：浏览器和 diff 组件都认它。
    """
    if isinstance(kind, AgentPatchKind):
        patch = session.execute(
            sa.select(PatchArtifact).where(
                PatchArtifact.evaluation_task_run_id == task_run_id,
                PatchArtifact.kind == PatchKind(kind.value),
            )
        ).scalar_one_or_none()
        if patch is None:
            return None
        return patch.uri, "text/x-diff", patch.size_bytes, patch.sha256

    artifact = session.execute(
        sa.select(Artifact).where(
            Artifact.owner_type == ArtifactOwnerType.TASK_RUN,
            Artifact.owner_id == task_run_id,
            Artifact.kind == kind,
        )
    ).scalar_one_or_none()
    if artifact is None:
        return None
    return artifact.uri, artifact.content_type, artifact.size_bytes, artifact.sha256


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    """一块一块读，读完关掉。内存里同时只有一个块。"""
    try:
        while chunk := stream.read(_CHUNK_SIZE):
            yield chunk
    finally:
        stream.close()


__all__ = [
    "AgentPatchKind",
    "ArtifactSummary",
    "FailureAttributionSummary",
    "PatchSummary",
    "TaskRunDetail",
    "TestResultRow",
    "router",
]
