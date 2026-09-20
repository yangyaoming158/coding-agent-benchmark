"""数据集版本化与发布（E1-T6，`03-benchmark-spec.md` §7.5 + 协议 C-50）。

    python -m cli.dataset stage --dataset-id benchmark-dev   # 冻快照，出一版 DRAFT
    python -m cli.dataset gate  --slug benchmark-dev         # 建 Oracle / Noop 两个门禁实验
    make worker                                              # 跑（几分钟，后台跑）
    python -m cli.dataset publish --slug benchmark-dev       # 查门禁 → 过了才发布
    python -m cli.dataset show                               # 看有哪些版本
    python -m cli.dataset verify --slug benchmark-dev --version v1   # 已发布版本的漂移检查
    python -m cli.dataset quarantine --task X --reason "..." # 隔离一道题

## 为什么是三步而不是一步

因为门禁要真的跑一遍评测，而评测由 Worker 进程跑、要几分钟。`publish` 不能替你等。

三步之间的接力靠**快照摘要**：`stage` 冻题时算一个，`gate` 把它写进两个实验的
`manifest`，`publish` 再算一遍要求三者一致。中间有人加题、改题、隔离题，
摘要就变了，旧的门禁结果作废。所以"先建 set 再跑门禁"不等于"先发布再检查" ——
发布这件事从头到尾只由 `benchmark_sets.status` 表示，而它只在门禁通过时才被写。

## 这个模块为什么在 cli/ 而不是 app/

门禁判定要读 `evaluation_runs`，建门禁实验要用 `app.evaluation.orchestrator`，
挑题冻表要用 `app.benchmark.dataset`。而 import-linter 的契约里
`app.evaluation | app.benchmark` 是"互不可见"的，`app` 里没有一个地方能同时 import 两边。
CLI 在 `app` 外面，是唯一的组合层 —— `cli/promote.py` 也是这么摆的。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.dataset import (
    MANIFEST_DIGEST_KEY,
    NOOP_AGENT,
    ORACLE_AGENT,
    DatasetError,
    GateVerdict,
    build_manifest,
    drift,
    evidence_of,
    export_lines,
    freeze,
    gate_verdict,
    items_of,
    plan_stage,
    resolve_set,
    select_tasks,
    sha256_of,
    snapshot_digest,
    versions_of,
)
from app.benchmark.hashing import to_bare_hex
from app.domain.enums import (
    BenchmarkSetStatus,
    EvaluationRunStatus,
    TaskValidationState,
)
from app.evaluation.agent_configs import AgentConfigError, resolve_agent_config
from app.evaluation.manifest import ProvenanceError, collect_provenance
from app.evaluation.orchestrator import OrchestrationError, create_runs
from app.infrastructure.config import REPO_ROOT, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.gitmeta import git_state
from app.infrastructure.models.agent import AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun

#: 指纹文件的落点，**入库**（每份 2 KB 上下）。
MANIFEST_ROOT = REPO_ROOT / "datasets" / "manifests"
#: 完整题目导出的落点，**不入库** —— 里面有 `gold_patch`（协议 C-44），
#: `.gitignore` 里 `datasets/exports/` 已经挡住了。
EXPORT_ROOT = REPO_ROOT / "datasets" / "exports"

#: 门禁实验还活着的状态。这两个状态下再跑一次 `gate` 会把作业投两遍。
LIVE_RUN_STATES = (
    EvaluationRunStatus.DRAFT,
    EvaluationRunStatus.QUEUED,
    EvaluationRunStatus.RUNNING,
)


# ── stage ───────────────────────────────────────────────────


def cmd_stage(args: argparse.Namespace) -> int:
    slug = args.slug or args.dataset_id
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        selection = select_tasks(session, args.dataset_id)

        # 重算 content_hash 对不上就整批拒绝。冻一个假的"身份证"下去，
        # 以后 `verify` 报出来的漂移全是噪声，比不冻更糟。
        if selection.mismatched:
            print(f"拒绝冻快照：{len(selection.mismatched)} 道题的 content_hash 对不上\n")
            for bad in selection.mismatched:
                print(f"  {bad.task_id}")
                print(f"    库里那一列  {bad.stored}")
                print(f"    现算出来的  {bad.recomputed}")
            print("\nraw_definition 被改过而没有同步 content_hash，或者反过来。")
            print("先查清楚该信哪一份，别急着冻。")
            return 1

        if not selection.rows:
            print(f"{args.dataset_id} 下一道 VALID 的题都没有，没什么可冻的")
            if selection.excluded:
                print("这个 dataset 下的题：" + _counts(selection.excluded))
            return 1

        digest = selection.digest
        bare = to_bare_hex(digest)
        existing = versions_of(session, slug)
        plan = plan_stage(existing, bare)

        if plan.action == "noop":
            print(
                f"{slug} 的题和已发布的 {plan.version} 完全一样"
                f"（{len(selection.rows)} 道），不用出新版本"
            )
            print("要加题就先跑 `cli.promote assemble`，或者用 `dataset quarantine` 减题")
            return 0

        if plan.action == "refresh":
            target = existing[0]
            action = f"刷新草稿 {target.version}"
            if plan.same_as is not None:
                print(f"⚠ 注意：这批题和已发布的 {plan.same_as} 一模一样，出这一版没有新内容\n")
        else:
            target = BenchmarkSet(
                slug=slug,
                version=plan.version,
                title=args.title or f"{args.dataset_id} 数据集",
                status=BenchmarkSetStatus.DRAFT,
            )
            session.add(target)
            session.flush()
            action = f"新建 {plan.version}"

        target.source_dataset_id = args.dataset_id
        before = target.snapshot_digest
        freeze(session, target, selection.rows)

        print(f"{slug}@{target.version}（{action}）")
        print(f"  来源 dataset_id  {args.dataset_id}")
        print(f"  冻进快照        {len(selection.rows)} 道题")
        if selection.excluded:
            print(f"  排除            {_counts(selection.excluded)}")
        print(f"  快照摘要        {digest}")
        if before is not None and before != bare:
            print(f"  （原来是 sha256:{before[:12]}…，摘要变了，之前跑过的门禁作废）")
        print(f"\n下一步：python -m cli.dataset gate --slug {slug}")
    return 0


def _counts(counts: dict[str, int] | object) -> str:
    """把 `{"INVALID": 9}` 打成 `INVALID 9`。"""
    assert isinstance(counts, dict)
    return "、".join(f"{state} {n}" for state, n in sorted(counts.items()))


# ── gate ────────────────────────────────────────────────────


def _agent_config(session: Session, name: str) -> AgentConfig:
    """门禁只用 oracle / noop，各只有一份配置，按名字就能唯一选到。"""
    try:
        return resolve_agent_config(session, agent=name)
    except AgentConfigError as exc:
        raise DatasetError(str(exc)) from exc


def cmd_gate(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))

    with session_scope(factory) as session:
        try:
            dataset, _ = resolve_set(session, args.slug, args.version, prefer_draft=True)
        except DatasetError as exc:
            print(exc)
            return 1

        if dataset.status is not BenchmarkSetStatus.DRAFT:
            print(f"{dataset.slug}@{dataset.version} 已经是 {dataset.status.value}，不用再跑门禁")
            return 1

        rows = items_of(session, dataset.id)
        if not rows:
            print(f"{dataset.slug}@{dataset.version} 的快照是空的，先跑 `dataset stage`")
            return 1

        digest = snapshot_digest(rows)
        if dataset.snapshot_digest != to_bare_hex(digest):
            print("快照摘要和库里存的对不上 —— 有人绕过发布流程改了题目清单")
            print("重跑 `python -m cli.dataset stage` 把快照冻一遍")
            return 1

        live = _live_gate_runs(session, dataset.id, digest)
        if live and not args.force:
            names = "、".join(f"#{r.id}（{r.status.value}）" for r in live)
            print(f"这一份快照的门禁实验还在跑：{names}")
            print(f"看进度：python -m cli.experiment status --run {live[0].id}")
            print("确实要再投一遍就加 --force")
            return 1

        task_ids = [row.benchmark_task_id for row in rows]

        # 两个 Agent 配置**先一起查齐**再动手建实验。分开查的话，oracle 建完了
        # 才发现 noop 没配，这时 return 1 —— 而 `session_scope` 退出时照样提交，
        # 库里留下半套门禁：oracle 的作业已经投进队列，下次再跑 `gate` 又会因为
        # "门禁还在跑"被拦下。半套门禁比没有门禁更难收拾。
        try:
            configs = {name: _agent_config(session, name) for name in (ORACLE_AGENT, NOOP_AGENT)}
        except DatasetError as exc:
            print(exc)
            return 1

        created: list[tuple[str, int]] = []
        dirty = False
        for agent_name in (ORACLE_AGENT, NOOP_AGENT):
            # 快照摘要、harness 版本、镜像 digest 表全由 `collect_provenance()` 凑齐，
            # 再由 `create_runs()` 一次性写进 manifest。E5-T4 之前这里是手写
            # 三个键再赋给 `run.manifest` —— 那是 manifest 的第二个写入口，
            # 两个入口迟早会在"该记什么"上分岔。
            #
            # 协议 C-27 的强制也在 `collect_provenance()` 里：门禁实验是"凭它发布
            # 数据集"的记录，脏工作区上跑出来的门禁，`publish` 本来也不收
            try:
                provenance = collect_provenance(
                    session,
                    benchmark_set_id=dataset.id,
                    snapshot_digest=digest,
                    agent_config_id=configs[agent_name].id,
                    task_ids=task_ids,
                    agent_concurrency=settings.agent_concurrency,
                    sandbox_concurrency=settings.sandbox_concurrency,
                    job_max_attempts=settings.job_max_attempts,
                    allow_dirty=args.allow_dirty,
                    gate_for=f"{dataset.slug}@{dataset.version}",
                )
                runs = create_runs(
                    session,
                    name=f"{dataset.slug}@{dataset.version} 发布门禁 · {agent_name}",
                    task_ids=task_ids,
                    provenance=provenance,
                )
            except (OrchestrationError, ProvenanceError) as exc:
                print(f"建不了 {agent_name} 门禁实验：{exc}")
                return 1
            dirty = provenance.dirty
            created.append((agent_name, runs[0].id))

        print(f"{dataset.slug}@{dataset.version} 的门禁实验已建（{len(task_ids)} 道题 × 2）：")
        for agent_name, run_id in created:
            print(f"  {agent_name:<7} 实验 #{run_id}")
        print(f"  快照摘要  {digest}")
        if dirty:
            print("\n⚠ 工作区有未提交改动，两次门禁实验都标了 dirty=true（协议 C-28）。")
            print("  发布时也得给 `publish --allow-dirty`，指纹里会如实记着。")
        print("\n下一步：make worker    跑完之后 python -m cli.dataset publish --slug " + args.slug)
    return 0


def _live_gate_runs(session: Session, benchmark_set_id: int, digest: str) -> list[EvaluationRun]:
    """这一份快照上还没跑完的门禁实验。"""
    return list(
        session.execute(
            sa.select(EvaluationRun)
            .where(
                EvaluationRun.benchmark_set_id == benchmark_set_id,
                EvaluationRun.manifest[MANIFEST_DIGEST_KEY].astext == digest,
                EvaluationRun.status.in_(LIVE_RUN_STATES),
            )
            .order_by(EvaluationRun.id)
        ).scalars()
    )


# ── publish ─────────────────────────────────────────────────


def _print_verdict(verdict: GateVerdict) -> None:
    for sentinel, want in ((verdict.oracle, "100%"), (verdict.noop, "0%")):
        if sentinel is None:
            continue
        rate = sentinel.resolved_count / sentinel.total_tasks if sentinel.total_tasks else 0.0
        print(
            f"  {sentinel.agent_name:<7} 实验 #{sentinel.run_id}  "
            f"{sentinel.resolved_count}/{sentinel.total_tasks} = {rate:.1%}（要求 {want}）  "
            f"{sentinel.status.value}"
        )


def cmd_publish(args: argparse.Namespace) -> int:
    factory = create_session_factory(create_db_engine())
    sha, dirty = git_state()

    with session_scope(factory) as session:
        try:
            dataset, _ = resolve_set(session, args.slug, args.version, prefer_draft=True)
        except DatasetError as exc:
            print(exc)
            return 1

        label = f"{dataset.slug}@{dataset.version}"
        if dataset.status is BenchmarkSetStatus.PUBLISHED:
            when = (
                f"{dataset.published_at:%Y-%m-%d %H:%M}"
                if dataset.published_at is not None
                else "时间不详"
            )
            print(f"{label} 已经发布过了（{when}）")
            print(f"指纹：{_shown(_manifest_path(dataset))}")
            print("要出新一版：改完题之后跑 `python -m cli.dataset stage`")
            return 0
        if dataset.status is not BenchmarkSetStatus.DRAFT:
            print(f"{label} 的状态是 {dataset.status.value}，发布不了")
            return 1

        verdict = gate_verdict(session, dataset)
        print(f"{label} 发布门禁（协议 C-50）：")
        _print_verdict(verdict)
        if not verdict.ok:
            print("\n不达标，拒绝发布：")
            for problem in verdict.problems:
                print(f"  · {problem}")
            return 1

        if dirty and not args.allow_dirty:
            print("\n工作区有未提交改动，拒绝发布（协议 C-27）：")
            print("  指纹里的 harness_git_sha 只有在工作区干净时才唯一代表代码状态。")
            print("  先提交，或者 `--allow-dirty` 放行（指纹里会如实标 dirty: true）。")
            return 1

        rows = items_of(session, dataset.id)
        dataset.status = BenchmarkSetStatus.PUBLISHED
        dataset.published_at = datetime.now(UTC)
        dataset.publish_evidence = evidence_of(verdict)
        session.flush()

        export_text = "".join(f"{line}\n" for line in export_lines(session, rows))
        manifest = build_manifest(
            dataset,
            rows,
            dataset_sha256=sha256_of(export_text),
            harness_git_sha=sha,
            dirty=dirty,
            evidence=dataset.publish_evidence or {},
        )
        export_path = _export_path(dataset)
        manifest_path = _manifest_path(dataset)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(export_text, encoding="utf-8")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        print(f"\n✅ 已发布 {label}，{len(rows)} 道题")
        print(f"  指纹（入库）    {_shown(manifest_path)}")
        size_kb = len(export_text.encode("utf-8")) // 1024
        print(f"  导出（不入库）  {_shown(export_path)}  {size_kb} KB")
        print(f"  题目清单哈希    {manifest['task_hashes_sha256']}")
        print(f"  导出文件哈希    {manifest['dataset_sha256']}")
        if dirty:
            print("  ⚠ dirty: true —— 这一版的门禁是在有未提交改动的工作区上跑的")
    return 0


def _shown(path: Path) -> str:
    """打印路径时优先用仓库相对写法，落在仓库外就打全路径。

    不能直接 `relative_to(REPO_ROOT)`：路径被配到仓库外时它会抛 ValueError，
    而那是**发布成功之后**才执行的一行打印 —— 库已经写了、文件已经落了，
    却以异常收场，看起来像发布失败了。
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _manifest_path(dataset: BenchmarkSet) -> Path:
    return MANIFEST_ROOT / f"{dataset.slug}@{dataset.version}.json"


def _export_path(dataset: BenchmarkSet) -> Path:
    return EXPORT_ROOT / f"{dataset.slug}@{dataset.version}.jsonl"


# ── show ────────────────────────────────────────────────────


def cmd_show(args: argparse.Namespace) -> int:
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        query = sa.select(BenchmarkSet).order_by(BenchmarkSet.slug, BenchmarkSet.id)
        if args.slug:
            query = query.where(BenchmarkSet.slug == args.slug)
        sets = list(session.execute(query).scalars())
        if not sets:
            print("库里一个数据集版本都没有，先跑 `python -m cli.dataset stage --dataset-id <id>`")
            return 1

        print(f"{'版本':<26} {'状态':<10} {'题数':>5}  {'来源 dataset_id':<16} 摘要")
        for row in sets:
            digest = f"sha256:{row.snapshot_digest[:12]}…" if row.snapshot_digest else "—"
            print(
                f"{row.slug + '@' + row.version:<26} {row.status.value:<10} "
                f"{row.task_count:>5}  {row.source_dataset_id or '—':<16} {digest}"
            )

        if args.slug and args.tasks:
            dataset, _ = resolve_set(session, args.slug, args.version)
            print(f"\n{dataset.slug}@{dataset.version} 的题目清单：")
            for index, item in enumerate(items_of(session, dataset.id), start=1):
                print(f"  {index:>3}. {item.task_id:<26} {item.content_hash[:12]}…")
            if dataset.publish_evidence:
                print("\n发布证据：")
                print(json.dumps(dataset.publish_evidence, ensure_ascii=False, indent=2))
    return 0


# ── verify ──────────────────────────────────────────────────


def cmd_verify(args: argparse.Namespace) -> int:
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        try:
            dataset, note = resolve_set(session, args.slug, args.version)
        except DatasetError as exc:
            print(exc)
            return 1
        if note:
            print(note)

        rows = items_of(session, dataset.id)
        label = f"{dataset.slug}@{dataset.version}"
        print(f"{label}：{len(rows)} 道题，状态 {dataset.status.value}")

        recomputed = to_bare_hex(snapshot_digest(rows))
        if dataset.snapshot_digest is None:
            print(f"  ⚠ 库里没存快照摘要（0001 建的老行），只能现算 sha256:{recomputed[:12]}…")
        elif recomputed != dataset.snapshot_digest:
            print(
                f"  ❌ 快照摘要对不上：存的 {dataset.snapshot_digest[:12]}…，"
                f"现算 {recomputed[:12]}…"
            )
            print("     题目清单被绕过发布流程改过")
            return 1
        else:
            print(f"  ✅ 快照摘要一致  sha256:{recomputed[:12]}…")

        result = drift(session, dataset.id)
        if result.clean:
            print("  ✅ 和现在的题库逐题一致，没有漂移")
            return 0

        # 三类漂移的处置完全不同，所以分开报
        if result.changed:
            print(f"\n  内容变了（{len(result.changed)} 道）—— 这一版不再等于现在的库：")
            for task_id in result.changed:
                print(f"    {task_id}")
        if result.quarantined:
            print(f"\n  已被隔离（{len(result.quarantined)} 道）—— 这一版不动，下一版会自动排除：")
            for task_id in result.quarantined:
                print(f"    {task_id}")
        if result.missing:
            print(f"\n  题不见了（{len(result.missing)} 道）—— 外键是 RESTRICT，正常删不掉：")
            for note_text in result.missing:
                print(f"    {note_text}")
        return 1


# ── quarantine ──────────────────────────────────────────────


def cmd_quarantine(args: argparse.Namespace) -> int:
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        task = session.execute(
            sa.select(BenchmarkTask).where(BenchmarkTask.task_id == args.task)
        ).scalar_one_or_none()
        if task is None:
            print(f"库里没有 {args.task} 这道题")
            return 1
        if task.validation_state is TaskValidationState.QUARANTINED:
            print(f"{args.task} 已经是 QUARANTINED 了")
            return 0

        before = task.validation_state
        task.validation_state = TaskValidationState.QUARANTINED
        # 理由写进 `quarantine` 那一列，**不碰 `raw_definition`**。
        #
        # 往 raw_definition 里加键会出两件事，两件都是静默的：① 它是
        # `TaskDefinition` 的原文而后者 `extra="forbid"`，加完之后这道题再也解析
        # 不回来，`cli.validate run` 直接崩；② `content_hash` 算的就是
        # raw_definition，加东西等于悄悄改了题目的身份证，而库里那一列没跟着变，
        # 下次 `dataset stage` 会把它报成"哈希对不上"。2026-09-10 第一版就这么写的，
        # 当场撞上①。
        task.quarantine = {
            "at": datetime.now(UTC).isoformat(),
            "from_state": before.value,
            "reason": args.reason,
        }
        session.flush()

        print(f"{args.task}：{before.value} → QUARANTINED")
        print(f"  理由：{args.reason}")
        print("\n  content_hash 不变：隔离是题目的状态，不是题目的内容")

        affected = _published_containing(session, task.id)
        if affected:
            print(f"\n  已发布的版本里有 {len(affected)} 版包含这道题，它们**一行都不改**：")
            for row in affected:
                print(f"    {row.slug}@{row.version}（{row.task_count} 道）")
            print("  下一次 `dataset stage` 出的新版本会自动排除它。")
        print("\n  查影响：python -m cli.dataset verify --slug <slug> --version <vN>")
    return 0


def _published_containing(session: Session, benchmark_task_id: int) -> list[BenchmarkSet]:
    """哪些已发布的版本里冻了这道题。"""
    from app.infrastructure.models.benchmark import BenchmarkSetItem

    return list(
        session.execute(
            sa.select(BenchmarkSet)
            .join(BenchmarkSetItem, BenchmarkSetItem.benchmark_set_id == BenchmarkSet.id)
            .where(
                BenchmarkSetItem.benchmark_task_id == benchmark_task_id,
                BenchmarkSet.status == BenchmarkSetStatus.PUBLISHED,
            )
            .order_by(BenchmarkSet.id)
        ).scalars()
    )


# ── 命令行 ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.dataset", description="数据集版本化与发布（E1-T6）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser("stage", help="冻快照，出一版 DRAFT")
    p_stage.add_argument(
        "--dataset-id",
        required=True,
        help="按 raw_definition->>'dataset_id' 挑题，比如 benchmark-dev",
    )
    p_stage.add_argument("--slug", help="数据集 slug，默认和 --dataset-id 同名")
    p_stage.add_argument("--title", help="数据集标题，只在新建版本时用")
    p_stage.set_defaults(func=cmd_stage)

    p_gate = sub.add_parser("gate", help="建 Oracle / Noop 两个门禁实验并投队列")
    p_gate.add_argument("--slug", required=True)
    p_gate.add_argument("--version", help="默认取最新的那一版")
    p_gate.add_argument("--force", action="store_true", help="已经有门禁在跑也照投")
    p_gate.add_argument(
        "--allow-dirty",
        action="store_true",
        help="工作区不干净也建门禁（协议 C-28：实验标 dirty=true，publish 也要给这个参数）",
    )
    p_gate.set_defaults(func=cmd_gate)

    p_publish = sub.add_parser("publish", help="查门禁，过了才发布")
    p_publish.add_argument("--slug", required=True)
    p_publish.add_argument("--version", help="默认取最新的那一版")
    p_publish.add_argument(
        "--allow-dirty", action="store_true", help="工作区不干净也发布（指纹里标 dirty）"
    )
    p_publish.set_defaults(func=cmd_publish)

    p_show = sub.add_parser("show", help="看有哪些版本")
    p_show.add_argument("--slug", help="只看这一个 slug")
    p_show.add_argument("--version", help="配合 --tasks 用")
    p_show.add_argument("--tasks", action="store_true", help="连题目清单一起打印")
    p_show.set_defaults(func=cmd_show)

    p_verify = sub.add_parser("verify", help="拿快照比对现在的题库，报漂移")
    p_verify.add_argument("--slug", required=True)
    p_verify.add_argument("--version", help="默认取最新的那一版")
    p_verify.set_defaults(func=cmd_verify)

    p_quarantine = sub.add_parser("quarantine", help="隔离一道题（下一版排除，已发布版本不动）")
    p_quarantine.add_argument("--task", required=True, help="task_id")
    p_quarantine.add_argument("--reason", required=True, help="为什么隔离，原文记进题目")
    p_quarantine.set_defaults(func=cmd_quarantine)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except DatasetError as exc:
        print(exc)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "git_state", "main"]
