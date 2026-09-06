"""把一次评测的结果写进数据库（E4-T4 的落库那一半）。

和 `app.evaluation.task_run` 分开是有意的：

- 编排那半要 Docker、要 git 镜像，但**不需要数据库**；
- 落库这半要数据库，但**不需要 Docker**。

合成一个函数的话，任何一条测试都得同时具备两样东西，本地少起一个就整片跳过 ——
而跳过是不报错的，看起来像全过了。

## 写哪几张表

| 表 | 写什么 |
|:---|:---|
| `evaluation_task_runs` | 三字段结论、各阶段时刻、补丁统计、F2P/P2P 计数、C-08b 的三个诊断字段 |
| `test_results` | 逐条用例（含 `MISSING`）—— "结论可查"的基础 |
| `patch_artifacts` | 原始补丁和标准化补丁**两份** |
| `artifacts` | Agent 日志、测试容器日志、测试报告 |

## 三字段组合在这里还会再校验一次

`judge()` 出口处已经过了一遍 `assert_legal_combination`（C-78），数据库那边还有一条
`legal_combination` CHECK 约束。**三道防线是刻意的**：判定引擎管住"我们算出来的
结论合法"，数据库管住"任何路径写进来的都合法"——包括以后可能出现的手工修数据、
数据迁移、别的服务写入。

协议 C-78 的原话是"必须在写库前抛出来，禁止静默落库"。真让一条非法组合落了库，
排行榜会算出一个谁也解释不了的数字，而且事后无法区分是判定错了还是写库错了。

## 状态只能往前走

协议 C-32：重试的做法是**新建一条记录**（`attempt_no` 加 1），
**禁止**把已有记录的状态改回去。所以这里只有"把一条 QUEUED/RUNNING 的记录推到终态"，
没有任何"改回去"的路径。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.enums import ArtifactOwnerType, PatchKind, TestRole
from app.domain.protocol import assert_legal_combination
from app.evaluation.task_run import TaskRunOutcome
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.evaluation import EvaluationTaskRun, PatchArtifact, TestResult
from app.judge.decision import CaseVerdict


def _count(cases: Sequence[CaseVerdict], role: TestRole) -> tuple[int, int]:
    """(通过数, 总数)。分母是**题目列出的条数**，不是报告里出现的条数 ——
    `MISSING` 也要占分母，否则"用例找不到"会让分母缩水，通过率反而变好看。"""
    picked = [c for c in cases if c.role is role]
    return sum(1 for c in picked if c.passed), len(picked)


def persist_task_run(
    session: Session,
    task_run: EvaluationTaskRun,
    outcome: TaskRunOutcome,
    *,
    flush: bool = True,
) -> EvaluationTaskRun:
    """把 `TaskRunOutcome` 写进 `task_run` 这一行，并补齐它的从表。

    `task_run` 必须是**已经存在于会话里**的记录（由 E5 的队列层在领取作业时建），
    这里只负责把它从非终态推到终态。不自己建记录是因为 `evaluation_run_id` 和
    `benchmark_task_id` 这两个外键属于编排层的知识。

    不 commit —— 事务边界归调用方。一次评测的落库要和"把作业标记成已完成"在同一个
    事务里，分开提交会出现"结果写了但作业还挂着"的中间态。
    """
    verdict = outcome.verdict
    timings = outcome.timings

    # C-78：写库前再校验一次。判定引擎已经查过，这里是第二道 ——
    # 以后可能有别的路径往这张表写，那时这道校验就是唯一挡得住的
    assert_legal_combination(
        verdict.lifecycle_status,
        verdict.infra_outcome,
        verdict.agent_outcome,
        timings.agent_started_at is not None,
    )

    task_run.lifecycle_status = verdict.lifecycle_status
    task_run.infra_outcome = verdict.infra_outcome
    task_run.agent_outcome = verdict.agent_outcome

    task_run.prepare_started_at = timings.prepare_started_at
    task_run.agent_started_at = timings.agent_started_at
    task_run.agent_finished_at = timings.agent_finished_at
    task_run.test_started_at = timings.test_started_at
    task_run.test_finished_at = timings.test_finished_at
    task_run.judged_at = timings.judged_at
    task_run.completed_at = timings.completed_at
    task_run.agent_duration_ms = timings.agent_duration_ms
    task_run.test_duration_ms = timings.test_duration_ms
    task_run.total_duration_ms = timings.total_duration_ms

    agent = outcome.agent_result
    if agent is not None:
        task_run.exit_code = agent.exit_code
        task_run.cost_source = agent.cost_source
        task_run.cost_usd = None if agent.cost_usd is None else Decimal(str(agent.cost_usd))
        task_run.turns = agent.turns
        if agent.token_usage is not None:
            task_run.tokens_input = agent.token_usage.input
            task_run.tokens_output = agent.token_usage.output
            # 缓存命中是 input 的一部分，不进 total（协议 §9.2 的 token_usage 语义）
            task_run.tokens_cache_read = agent.token_usage.cache_read
            task_run.tokens_total = agent.token_usage.total

    patch = outcome.patch
    if patch is not None:
        task_run.files_changed = patch.stats.files_changed
        task_run.lines_added = patch.stats.lines_added
        task_run.lines_deleted = patch.stats.lines_deleted
        # C-08b 的三个诊断字段。少了它们，"AI 什么都没做"和
        # "AI 改的全是受保护文件、被我们丢光了"在数据上完全一样
        task_run.raw_patch_empty = patch.raw_patch_empty
        task_run.protected_path_edit_attempted = patch.protected_path_edit_attempted
        task_run.filtered_change_reasons = patch.filtered_change_reasons()  # type: ignore[assignment]

    # 执行器那边也可能发现 AI 碰了受保护路径（第二道防线抓到的），两边取或
    if outcome.execution is not None and outcome.execution.restore.attempted:
        task_run.protected_path_edit_attempted = True

    f2p_passed, f2p_total = _count(verdict.cases, TestRole.F2P)
    p2p_passed, p2p_total = _count(verdict.cases, TestRole.P2P)
    task_run.f2p_passed, task_run.f2p_total = f2p_passed, f2p_total
    task_run.p2p_passed, task_run.p2p_total = p2p_passed, p2p_total

    task_run.error_code = outcome.error_code
    task_run.error_message_excerpt = outcome.error_message_excerpt

    if flush:
        # 先 flush 拿到自增 id —— 下面三张从表都要拿它当外键
        session.flush()

    _write_test_results(session, task_run, verdict.cases)
    _write_patch_artifacts(session, task_run, outcome)
    _write_artifacts(session, task_run, outcome)
    if flush:
        session.flush()
    return task_run


def _write_test_results(
    session: Session, task_run: EvaluationTaskRun, cases: Sequence[CaseVerdict]
) -> None:
    """逐条用例入库 —— 判定的证据。

    **`MISSING` 的那些也要写。** 它们正是复核任务要看的东西（C-13b 第 2 项要求
    把题目里的 ID 和报告里的 ID 摆在一起对照），不写的话查起来只剩一个数字对不上。
    """
    for case in cases:
        session.add(
            TestResult(
                evaluation_task_run_id=task_run.id,
                test_id=case.test_id,
                role=case.role,
                status=case.status,
                duration_ms=case.duration_ms,
                message_excerpt=case.message_excerpt,
            )
        )


def _write_patch_artifacts(
    session: Session, task_run: EvaluationTaskRun, outcome: TaskRunOutcome
) -> None:
    """两份补丁都写。只写标准化的话，"AI 试图改测试文件"就再也查不到了（C-08b）。"""
    patch = outcome.patch
    if patch is None:
        return
    stats_by_kind = {
        PatchKind.AGENT_RAW: patch.raw_stats,
        PatchKind.AGENT_NORMALIZED: patch.stats,
    }
    for kind, ref in outcome.patches.items():
        stats = stats_by_kind.get(kind)
        if stats is None:
            continue
        session.add(
            PatchArtifact(
                evaluation_task_run_id=task_run.id,
                kind=kind,
                uri=ref.uri,
                sha256=ref.sha256,
                size_bytes=ref.size_bytes,
                files_changed=stats.files_changed,
                lines_added=stats.lines_added,
                lines_deleted=stats.lines_deleted,
                is_empty=stats.is_empty,
            )
        )
        if kind is PatchKind.AGENT_NORMALIZED:
            session.flush()
            task_run.patch_artifact_id = (
                session.query(PatchArtifact.id)
                .filter_by(evaluation_task_run_id=task_run.id, kind=kind)
                .scalar()
            )


def _write_artifacts(
    session: Session, task_run: EvaluationTaskRun, outcome: TaskRunOutcome
) -> None:
    """日志类制品只在库里留索引行，内容在制品存储里（可达数 MB，不入库）。"""
    for kind, ref in outcome.artifacts.items():
        session.add(
            Artifact(
                owner_type=ArtifactOwnerType.TASK_RUN,
                owner_id=task_run.id,
                kind=kind,
                uri=ref.uri,
                backend=ref.backend,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
                compressed=ref.compressed,
            )
        )


__all__ = ["persist_task_run"]
