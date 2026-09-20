"""归因的查库与落库（E6-T1 / E6-T2）。

分工和 `app.analytics.leaderboard` 一样：**判定口径在纯函数里，这里只管取数和写数。**

    load_facts(session)                 → 每次运行一份 RunFacts（一条 SQL 取完）
    existing_stages(session)            → 已经有结论的运行，以及是哪一层给的
    save_rule_verdicts(session, items)  → upsert，不碰 LLM/HUMAN 的结论
    collect_features(session, store, …) → 一次运行的 Stage2 特征
    load_llm_input(session, store, id)   → 裁剪后的 LLM 输入，不含官方代码
    save_llm_decisions(session, items)  → upsert，不碰 RULE/HUMAN 的结论

## 为什么不覆盖 LLM / HUMAN 的结论

`failure_attributions` 上有 `UNIQUE(evaluation_task_run_id)`，一次运行只有一行。
规则层重跑一遍就把人工抽检改过的结论盖掉，那抽检等于白做。

落法是 `ON CONFLICT … DO UPDATE … WHERE stage = 'RULE'` —— **条件写在 SQL 里**，
不是先查一遍再决定写不写。后者在并发下会漏（查到的是 RULE，写进去时已经被人改成 HUMAN 了）。
被 WHERE 挡下来的行不会出现在 `RETURNING` 里，所以顺便就数出来"保护了几行"。

## 特征取不到不抛异常

四组特征各读各的东西：补丁读制品库、报错对比读 Noop 哨兵的用例、日志和轨迹
读制品库。任何一处读不到，都只把维度名记进 `unavailable`，**不让整次归因失败**。

理由是 §12.4 最后一条：归因和判定是两件事，归因挂了不能影响判定结果。
这里更进一步 —— 一组特征取不到，也不该毁掉另外三组。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.attribution.features import (
    LogErrors,
    MessageShift,
    PatchOverlap,
    Stage2Features,
    TrajectoryStats,
    log_errors,
    message_shift,
    patch_overlap,
    trajectory_stats,
)
from app.attribution.llm import AttributionDecision, AttributionInput, FailedTest
from app.attribution.rules import RuleVerdict, RunFacts
from app.domain.enums import (
    AgentKind,
    ArtifactKind,
    ArtifactOwnerType,
    AttributionStage,
    AttributionStatus,
    HumanReviewAction,
    PatchKind,
    TestRole,
    TestStatus,
)
from app.domain.patch_paths import derive_patch_paths
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import FailureAttribution, HumanReview
from app.infrastructure.models.benchmark import BenchmarkTask
from app.infrastructure.models.evaluation import (
    EvaluationRun,
    EvaluationTaskRun,
    PatchArtifact,
    TestResult,
)
from app.storage.base import ArtifactStore, key_from_uri


@dataclass(frozen=True)
class RunRow:
    """一次运行：库里的主键 + 规则分类器要的那些字段。"""

    task_run_id: int
    facts: RunFacts


@dataclass(frozen=True)
class SaveReport:
    """落库结果。`protected` 是被 LLM/HUMAN 结论挡下来、没写成的行数。"""

    inserted: int
    updated: int
    protected: int


def load_facts(session: Session, *, task_run_ids: Sequence[int] | None = None) -> list[RunRow]:
    """把要归因的运行连同判据字段一次取回来。

    不给 `task_run_ids` 就是全库。字段全在 `evaluation_task_runs` 一张表上，
    所以这里没有 join，也不会有 N+1。
    """
    stmt = sa.select(
        EvaluationTaskRun.id,
        EvaluationTaskRun.infra_outcome,
        EvaluationTaskRun.agent_outcome,
        EvaluationTaskRun.f2p_passed,
        EvaluationTaskRun.f2p_total,
        EvaluationTaskRun.p2p_passed,
        EvaluationTaskRun.p2p_total,
        EvaluationTaskRun.raw_patch_empty,
        EvaluationTaskRun.protected_path_edit_attempted,
    ).order_by(EvaluationTaskRun.id)
    if task_run_ids is not None:
        stmt = stmt.where(EvaluationTaskRun.id.in_(task_run_ids))
    return [
        RunRow(
            task_run_id=row.id,
            facts=RunFacts(
                infra_outcome=row.infra_outcome,
                agent_outcome=row.agent_outcome,
                f2p_passed=row.f2p_passed,
                f2p_total=row.f2p_total,
                p2p_passed=row.p2p_passed,
                p2p_total=row.p2p_total,
                raw_patch_empty=row.raw_patch_empty,
                protected_path_edit_attempted=row.protected_path_edit_attempted,
            ),
        )
        for row in session.execute(stmt)
    ]


def existing_stages(session: Session) -> dict[int, AttributionStage]:
    """已经有归因结论的运行 → 结论是哪一层给的。"""
    stmt = sa.select(FailureAttribution.evaluation_task_run_id, FailureAttribution.stage)
    return {row[0]: row[1] for row in session.execute(stmt)}


def save_rule_verdicts(session: Session, items: Iterable[tuple[int, RuleVerdict]]) -> SaveReport:
    """把规则结论 upsert 进 `failure_attributions`。

    已有 `stage = LLM / HUMAN` 的行一律不动（条件在 SQL 的 WHERE 上，见模块文档）。
    """
    rows = [
        {
            "evaluation_task_run_id": task_run_id,
            "stage": AttributionStage.RULE,
            "category": verdict.category,
            "evidence": {"rule": verdict.rule.value, "facts": verdict.evidence},
            "status": AttributionStatus.OK,
        }
        for task_run_id, verdict in items
    ]
    if not rows:
        return SaveReport(inserted=0, updated=0, protected=0)

    # 先看一眼谁已经有结论 —— 这一步必须在 upsert **之前**，
    # 写完再查的话每一行看上去都已经是 RULE 了，"新插"和"更新"就分不开。
    before = existing_stages(session)

    insert = pg_insert(FailureAttribution).values(rows)
    reviewed = sa.exists().where(
        HumanReview.evaluation_task_run_id == FailureAttribution.evaluation_task_run_id,
        HumanReview.action != HumanReviewAction.COMMENT,
    )
    stmt = insert.on_conflict_do_update(
        index_elements=[FailureAttribution.evaluation_task_run_id],
        set_={
            "stage": insert.excluded.stage,
            "category": insert.excluded.category,
            "evidence": insert.excluded.evidence,
            "status": insert.excluded.status,
        },
        # 第一张有效人工标签落下后，自动结论就是盲检的对照基线，不能再改。
        where=sa.and_(
            FailureAttribution.stage == AttributionStage.RULE,
            ~reviewed,
        ),
    ).returning(FailureAttribution.evaluation_task_run_id)

    # `RETURNING` 只吐真正写成的行；被 WHERE 挡下来的（LLM / HUMAN 的结论）不在里面。
    written = {row[0] for row in session.execute(stmt)}
    inserted = sum(
        1
        for row in rows
        if row["evaluation_task_run_id"] in written and row["evaluation_task_run_id"] not in before
    )
    return SaveReport(
        inserted=inserted,
        updated=len(written) - inserted,
        protected=len(rows) - len(written),
    )


def load_llm_input(
    session: Session, store: ArtifactStore, task_run_id: int
) -> AttributionInput | None:
    """组装 §12.3 规定的裁剪输入。

    官方补丁的正文只在这里用来数文件和行数，返回值里只有
    `gold_summary`。`AttributionInput` 根本没有官方补丁正文字段，所以后续
    拼 prompt 时不可能不小心把答案喂给模型。
    """
    task = session.execute(
        sa.select(
            BenchmarkTask.task_id,
            BenchmarkTask.issue_title,
            BenchmarkTask.issue_body,
            BenchmarkTask.raw_definition,
            BenchmarkTask.gold_patch_uri,
        )
        .join(EvaluationTaskRun, EvaluationTaskRun.benchmark_task_id == BenchmarkTask.id)
        .where(EvaluationTaskRun.id == task_run_id)
    ).one_or_none()
    if task is None:
        return None

    agent_uri = session.scalar(
        sa.select(PatchArtifact.uri)
        .where(
            PatchArtifact.evaluation_task_run_id == task_run_id,
            PatchArtifact.kind == PatchKind.AGENT_NORMALIZED,
        )
        .order_by(PatchArtifact.id.desc())
        .limit(1)
    )
    agent_patch = _read_text(store, agent_uri) if agent_uri else None
    gold_patch = _gold_patch(store, task.raw_definition, task.gold_patch_uri)
    failed_rows = session.execute(
        sa.select(TestResult.test_id, TestResult.status, TestResult.message_excerpt)
        .where(
            TestResult.evaluation_task_run_id == task_run_id,
            TestResult.role == TestRole.F2P,
            TestResult.status != TestStatus.PASSED,
        )
        .order_by(TestResult.id)
        .limit(3)
    )
    failed_tests = tuple(
        FailedTest(
            test_id=row.test_id,
            status=row.status.value,
            message=row.message_excerpt or "（无报错摘要）",
        )
        for row in failed_rows
    )
    return AttributionInput(
        task_run_id=task_run_id,
        task_id=task.task_id,
        issue_title=task.issue_title,
        issue_body=task.issue_body,
        agent_patch=agent_patch or "",
        gold_summary=_gold_summary(gold_patch),
        failed_tests=failed_tests,
        features=collect_features(session, store, task_run_id).to_dict(),
    )


def save_llm_decisions(
    session: Session, items: Iterable[tuple[int, AttributionDecision]]
) -> SaveReport:
    """落 LLM 结论，只允许覆盖旧的 LLM 结论。

    RULE 是确定性结论，HUMAN 是人工复核结论，两者优先级都高于 LLM。
    保护条件放在 `ON CONFLICT ... WHERE` 里，防止模型调用期间恰好有人
    提交复核、然后被迟到的 LLM 回答盖掉。
    """
    rows = []
    for task_run_id, decision in items:
        verdict = decision.verdict
        rows.append(
            {
                "evaluation_task_run_id": task_run_id,
                "stage": AttributionStage.LLM,
                "category": verdict.category,
                "secondary_category": verdict.secondary_category,
                "confidence": Decimal(str(verdict.confidence)),
                "judge_model": decision.judge_model,
                "prompt_hash": decision.prompt_hash,
                "evidence": {
                    "citations": [item.model_dump(mode="json") for item in verdict.evidence],
                    "vote_categories": decision.raw_response.get("vote_categories", []),
                },
                "reasoning_zh": verdict.reasoning_zh,
                "raw_response": decision.raw_response,
                "status": decision.status,
            }
        )
    if not rows:
        return SaveReport(inserted=0, updated=0, protected=0)

    before = existing_stages(session)
    insert = pg_insert(FailureAttribution).values(rows)
    reviewed = sa.exists().where(
        HumanReview.evaluation_task_run_id == FailureAttribution.evaluation_task_run_id,
        HumanReview.action != HumanReviewAction.COMMENT,
    )
    stmt = insert.on_conflict_do_update(
        index_elements=[FailureAttribution.evaluation_task_run_id],
        set_={
            "stage": insert.excluded.stage,
            "category": insert.excluded.category,
            "secondary_category": insert.excluded.secondary_category,
            "confidence": insert.excluded.confidence,
            "judge_model": insert.excluded.judge_model,
            "prompt_hash": insert.excluded.prompt_hash,
            "evidence": insert.excluded.evidence,
            "reasoning_zh": insert.excluded.reasoning_zh,
            "raw_response": insert.excluded.raw_response,
            "status": insert.excluded.status,
        },
        where=sa.and_(
            FailureAttribution.stage == AttributionStage.LLM,
            ~reviewed,
        ),
    ).returning(FailureAttribution.evaluation_task_run_id)
    written = {row[0] for row in session.execute(stmt)}
    inserted = sum(
        1
        for row in rows
        if row["evaluation_task_run_id"] in written and row["evaluation_task_run_id"] not in before
    )
    return SaveReport(
        inserted=inserted,
        updated=len(written) - inserted,
        protected=len(rows) - len(written),
    )


def collect_features(session: Session, store: ArtifactStore, task_run_id: int) -> Stage2Features:
    """一次运行的四组 Stage2 特征。任何一组取不到就标 unavailable。"""
    unavailable: dict[str, str] = {}
    overlap = _patch_overlap(session, store, task_run_id, unavailable)
    shift = _message_shift(session, task_run_id, unavailable)
    errors = _log_errors(session, store, task_run_id, unavailable)
    traj = _trajectory(session, store, task_run_id, unavailable)
    return Stage2Features(
        patch_overlap=overlap,
        message_shift=shift,
        log_errors=errors,
        trajectory=traj,
        unavailable=unavailable,
    )


def _patch_overlap(
    session: Session, store: ArtifactStore, task_run_id: int, unavailable: dict[str, str]
) -> PatchOverlap | None:
    agent_uri = session.scalar(
        sa.select(PatchArtifact.uri).where(
            PatchArtifact.evaluation_task_run_id == task_run_id,
            PatchArtifact.kind == PatchKind.AGENT_NORMALIZED,
        )
    )
    task = session.execute(
        sa.select(BenchmarkTask.raw_definition, BenchmarkTask.gold_patch_uri)
        .join(
            EvaluationTaskRun,
            EvaluationTaskRun.benchmark_task_id == BenchmarkTask.id,
        )
        .where(EvaluationTaskRun.id == task_run_id)
    ).one_or_none()
    if not agent_uri:
        unavailable["patch_overlap"] = "这次运行没有归一化补丁"
        return None
    if task is None:
        unavailable["patch_overlap"] = "找不到这道题"
        return None

    agent_diff = _read_text(store, agent_uri)
    if agent_diff is None:
        unavailable["patch_overlap"] = "AI 的补丁读不出来"
        return None
    gold_diff = _gold_patch(store, task.raw_definition, task.gold_patch_uri)
    if gold_diff is None:
        unavailable["patch_overlap"] = "题目没有官方补丁"
        return None
    return patch_overlap(derive_patch_paths(agent_diff), derive_patch_paths(gold_diff))


def _gold_patch(
    store: ArtifactStore, raw_definition: dict[str, object] | None, uri: str | None
) -> str | None:
    """官方补丁的正文。**先看 `raw_definition`，再看制品库。**

    `benchmark_tasks.gold_patch_uri` 是个占位符，不是真地址：`cli/queue.py` 入库时
    按 `<scheme>://<task_id>/gold.patch` 拼出来，注释写着"留给 E1-T3 落制品之后回填"，
    而那次回填没做。挖掘来的题上它长成 `mined://…`，制品库根本不认这个 scheme。

    补丁正文一直都在 `raw_definition` 里（`cli/queue.py` 存的是完整的 TaskDefinition），
    所以这里以它为准。留着制品库那条路，是为了将来真回填了之后不用再改这里。
    """
    if isinstance(raw_definition, dict):
        text = raw_definition.get("gold_patch")
        if isinstance(text, str) and text.strip():
            return text
    if uri:
        return _read_text(store, uri)
    return None


def _message_shift(
    session: Session, task_run_id: int, unavailable: dict[str, str]
) -> MessageShift | None:
    """基线报错取自同一份数据集快照的 Noop 哨兵运行，理由见 features 的模块文档。"""
    context = session.execute(
        sa.select(EvaluationTaskRun.benchmark_task_id, EvaluationRun.benchmark_set_id)
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .where(EvaluationTaskRun.id == task_run_id)
    ).one_or_none()
    if context is None:
        unavailable["message_shift"] = "找不到这次运行"
        return None

    baseline_run_id = session.scalar(
        sa.select(EvaluationTaskRun.id)
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .where(
            Agent.kind == AgentKind.NOOP,
            EvaluationTaskRun.benchmark_task_id == context.benchmark_task_id,
            EvaluationRun.benchmark_set_id == context.benchmark_set_id,
        )
        .order_by(EvaluationTaskRun.id.desc())
        .limit(1)
    )
    if baseline_run_id is None:
        unavailable["message_shift"] = "这份数据集快照没跑过 Noop 哨兵，没有基线报错"
        return None
    if baseline_run_id == task_run_id:
        unavailable["message_shift"] = "这次运行本身就是 Noop 基线"
        return None

    current = _f2p_messages(session, task_run_id)
    baseline = _f2p_messages(session, baseline_run_id)
    if not baseline:
        unavailable["message_shift"] = "Noop 基线里没有报错文本"
        return None
    return message_shift(current, baseline)


def _f2p_messages(session: Session, task_run_id: int) -> dict[str, str | None]:
    stmt = sa.select(TestResult.test_id, TestResult.message_excerpt).where(
        TestResult.evaluation_task_run_id == task_run_id,
        TestResult.role == TestRole.F2P,
    )
    return {row[0]: row[1] for row in session.execute(stmt)}


def _log_errors(
    session: Session, store: ArtifactStore, task_run_id: int, unavailable: dict[str, str]
) -> LogErrors | None:
    uri = _artifact_uri(session, task_run_id, ArtifactKind.TEST_STDOUT)
    if uri is None:
        unavailable["log_errors"] = "这次运行没有测试日志（多半没跑到测试就结束了）"
        return None
    text = _read_text(store, uri)
    if text is None:
        unavailable["log_errors"] = "测试日志读不出来"
        return None
    return log_errors(text)


def _trajectory(
    session: Session, store: ArtifactStore, task_run_id: int, unavailable: dict[str, str]
) -> TrajectoryStats | None:
    uri = _artifact_uri(session, task_run_id, ArtifactKind.TRAJECTORY)
    if uri is None:
        unavailable["trajectory"] = "这个适配器没有落轨迹"
        return None
    text = _read_text(store, uri)
    if text is None:
        unavailable["trajectory"] = "轨迹读不出来"
        return None
    return trajectory_stats(text.splitlines())


def _artifact_uri(session: Session, task_run_id: int, kind: ArtifactKind) -> str | None:
    return session.scalar(
        sa.select(Artifact.uri)
        .where(
            Artifact.owner_type == ArtifactOwnerType.TASK_RUN,
            Artifact.owner_id == task_run_id,
            Artifact.kind == kind,
        )
        .order_by(Artifact.id.desc())
        .limit(1)
    )


def _read_text(store: ArtifactStore, uri: str) -> str | None:
    """读一份制品。读不出来返回 None —— 归因不该因为少一个文件就整次失败。"""
    try:
        return store.get(key_from_uri(uri)).decode("utf-8", errors="replace")
    # 制品缺失、损坏、后端不通 —— 一律降级成"取不到"，不让归因整次失败
    except Exception:
        return None


def _gold_summary(diff: str | None) -> str:
    """官方补丁只转成文件清单与规模，不泄露任何代码。"""
    if not diff:
        return "（不可用）"
    paths = derive_patch_paths(diff)
    added = 0
    deleted = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    listed = "\n".join(f"- {path}" for path in paths) or "- （无可识别文件）"
    return f"文件数：{len(paths)}\n新增行：{added}\n删除行：{deleted}\n文件：\n{listed}"


__all__ = [
    "RunRow",
    "SaveReport",
    "collect_features",
    "existing_stages",
    "load_facts",
    "load_llm_input",
    "save_llm_decisions",
    "save_rule_verdicts",
]
