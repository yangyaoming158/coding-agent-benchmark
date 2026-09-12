"""实验的命令行：建、看、取消、补跑、导并发时序、看 manifest、重放（E5-T2、E5-T4）。

    python -m cli.experiment start --agent oracle --rounds 3   # 建 3 个实验并投作业
    python -m cli.experiment status                            # 看所有实验
    python -m cli.experiment status --run 12                   # 看一个实验的细账
    python -m cli.experiment cancel --run 12                   # 取消
    python -m cli.experiment retry-failed --run 12             # 把没结论的题补跑
    python -m cli.experiment concurrency --run 12 --run 13     # 有效并发时序
    python -m cli.experiment timing --run 12 --run 13          # 阶段耗时 + makespan 投影
    python -m cli.experiment manifest --run 12                 # 看可复现性清单
    python -m cli.experiment manifest --run 12 --run 13        # 比两次运行
    python -m cli.experiment replay --run 12                   # 按 manifest 重建一次等价运行

配合 `python -m app.worker` 用：一个终端起 Worker，另一个终端在这里操作。

## `replay` 重建的是输入条件，不是结果

`replay` 保证的是"这一次和那一次跑的是同一批题、同一个参赛者、同一套镜像、
同一版代码"，它**不保证**两次的解决率一样 —— 协议 C-73 写得很清楚：
测试执行的可复现性是目标不是保证。逐实例一致率是 MET-01 的口径，那是 E10-T5。

和 E3-T8 的 `ReplayRunner` 也不是一件事：那个喂的是**别人**发布的预测补丁文件，
输入根本不是我们的 manifest。

## 为什么不叫 `cli.run`

旁边已经有一个 `cli.runner`（Runner 协议的 schema 工具）。`cli.run` 和 `cli.runner`
只差一个字母，敲错了不会报错，只会跑到另一个命令上去。

## 多轮取样为什么是多个实验

`--rounds 3` 建的是 **3 个 `EvaluationRun`**，不是一个实验里跑 3 遍。
协议 C-55 要求人工重跑必须新建实验；C-57 的部分唯一索引也限死了
"每题至多一个认定结果"，同一个实验里跑两遍，第二遍的结论没地方放。

三个实验跑完之后，"同一个 AI 在同一批题上的解决率波动"就是这三个数的离散程度
（需求 §4.5 的方案 C）。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.dataset import (
    MANIFEST_DIGEST_KEY,
    DatasetError,
    drift,
    items_of,
    resolve_set,
    snapshot_digest,
)
from app.domain.makespan import (
    DEFAULT_OVERHEAD_RATIO,
    TARGET_HOURS,
    TARGET_RUNS,
    MakespanInputs,
    max_agent_minutes,
    measured_overhead,
    project,
)
from app.domain.manifest import VOLATILE_KEYS
from app.domain.protocol import PROTOCOL_VERSION
from app.evaluation import concurrency as concurrency_mod
from app.evaluation import progress as progress_mod
from app.evaluation import timing as timing_mod
from app.evaluation.manifest import (
    ManifestDiff,
    ProvenanceError,
    block,
    build_manifest,
    collect_provenance,
    diff_manifests,
    pinned_image_ref,
)
from app.evaluation.orchestrator import (
    OrchestrationError,
    cancel_run,
    create_runs,
    retry_failed,
)
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet
from app.infrastructure.models.evaluation import EvaluationRun
from app.infrastructure.models.job import JobQueue
from app.sandbox.container import ImageNotFoundError, SandboxError, inspect_image
from app.worker.cancel import RUN_ID_KEY

GOLDEN_SET_SLUG = "golden"


# ── start ───────────────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        config = session.execute(
            sa.select(AgentConfig).join(Agent).where(Agent.name == args.agent)
        ).scalar_one_or_none()
        if config is None:
            print(f"找不到 Agent {args.agent} 的配置，先跑 `make seed`")
            return 1

        # 题从**数据集快照**（`benchmark_set_items`）里取。理由和 `cli.queue enqueue`
        # 那一处一样：整张 benchmark_tasks 表里混着 INVALID 和别的数据集的题，
        # 全投进去的话哨兵解决率必然不对，而且分母也不是这个数据集的题数。
        try:
            dataset, note = resolve_set(session, args.set, args.version)
        except DatasetError as exc:
            print(exc)
            return 1
        if note:
            print(f"⚠ {note}")

        # 摘要算的是**整份快照**，不是 --task 选中的那几道：它回答的是
        # "这一版数据集是哪一版"。选了子集这件事由 dataset.selected_task_ids 记
        all_rows = items_of(session, dataset.id)
        rows = all_rows
        if args.task:
            wanted = set(args.task)
            rows = [row for row in all_rows if row.task_id in wanted]
        task_ids = [row.benchmark_task_id for row in rows]
        if not task_ids:
            print(
                f"{dataset.slug}@{dataset.version} 里没选中任何题目"
                "（检查 --task 拼写，或者先跑 `python -m cli.dataset stage`）"
            )
            return 1

        try:
            provenance = collect_provenance(
                session,
                benchmark_set_id=dataset.id,
                snapshot_digest=snapshot_digest(all_rows),
                agent_config_id=config.id,
                task_ids=task_ids,
                agent_concurrency=args.agent_concurrency or settings.agent_concurrency,
                sandbox_concurrency=args.sandbox_concurrency or settings.sandbox_concurrency,
                job_max_attempts=settings.job_max_attempts,
                allow_dirty=args.allow_dirty,
            )
            runs = create_runs(
                session,
                name=args.name,
                task_ids=task_ids,
                provenance=provenance,
                rounds=args.rounds,
            )
        except (OrchestrationError, ProvenanceError) as exc:
            print(f"建不了：{exc}")
            return 1
        created = [(run.id, run.name) for run in runs]
        dirty = provenance.dirty

    for run_id, name in created:
        print(f"实验 #{run_id}（{name}）已建，投了 {len(task_ids)} 条 EVAL_TASK 作业")
    print(f"共 {len(created)} 轮 × {len(task_ids)} 题 = {len(created) * len(task_ids)} 条作业")
    _warn_if_dirty(dirty)
    print("起 Worker 来跑：python -m app.worker")
    return 0


def _warn_if_dirty(dirty: bool) -> None:
    """脏工作区放行之后必须说一句（协议 C-28）。

    悄悄放行是最坏的：跑出来的数字看着和正式实验一模一样，
    而它记的 harness_git_sha 根本代表不了当时的代码。
    """
    if dirty:
        print("⚠ 工作区有未提交改动，这次实验已标 dirty=true（协议 C-28：不得进排行榜）")


# ── status ──────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        if args.run:
            return _status_detail(session, args.run)
        return _status_list(session, limit=args.limit)


def _status_list(session: Session, *, limit: int) -> int:
    rows = list(
        session.execute(
            sa.select(EvaluationRun, Agent.name)
            .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
            .join(Agent, Agent.id == AgentConfig.agent_id)
            .order_by(EvaluationRun.id.desc())
            .limit(limit)
        ).all()
    )
    if not rows:
        print("一个实验都还没有。建一个：python -m cli.experiment start --agent oracle")
        return 0

    print(
        f"{'#':>4}  {'Agent':<10} {'状态':<10} {'进度':>9} "
        f"{'解决':>7} {'故障':>5} {'成本':>10}  名字"
    )
    for run, agent_name in rows:
        done = f"{run.completed_tasks}/{run.total_tasks}"
        resolved = f"{run.resolved_count}/{run.total_tasks}"
        print(
            f"{run.id:>4}  {agent_name:<10} {run.status.value:<10} {done:>9} "
            f"{resolved:>7} {run.infra_failure_count:>5} "
            f"{float(run.total_cost_usd):>10.4f}  {run.name}"
        )
    return 0


def _status_detail(session: Session, run_id: int) -> int:
    run = session.get(EvaluationRun, run_id)
    if run is None:
        print(f"找不到实验 #{run_id}")
        return 1

    # 只读，不写回：`status --run` 不该改数据。要的是"现在算出来是什么样"
    attempts = progress_mod.load_attempts(session, run_id)
    live = progress_mod.live_job_count(session, run_id, exclude_job_id=None)
    snapshot = progress_mod.summarize(
        attempts,
        total_tasks=run.total_tasks,
        all_jobs_done=live == 0,
        current_status=run.status,
    )

    print(f"实验 #{run.id}  {run.name}")
    print(f"  状态          {run.status.value}（重算：{snapshot.status.value}）")
    print(
        f"  题数          {run.total_tasks}，有结论 {snapshot.completed_tasks}，"
        f"还在跑 {live} 条作业"
    )
    print(f"  解决          {snapshot.resolved_count} 题")
    print(f"  严格解决率    {_rate(snapshot.strict_resolve_rate)}   （C-21，分母是全部题数）")
    print(
        f"  有效解决率    {_rate(snapshot.effective_resolve_rate)}   "
        "（C-21，分母是可归因于 AI 的题数）"
    )
    print(
        f"  平台故障      {snapshot.infra_failure_count} 题"
        f"（准入上限 {run.total_tasks * 5 // 100}，C-26a）"
    )
    if snapshot.pending_control_run:
        print(
            f"                其中 {snapshot.pending_control_run} 题是测试超时，"
            "要跑 C-20 的对照组才能定责，现在保守算作平台故障"
        )
    print(
        f"  重试          {snapshot.retry_count} 次，"
        f"其中救回来 {snapshot.recovered_infra_failure_count} 次"
    )
    # 报不出成本的那几次要单独说。不说的话，一场全员 unavailable 的实验
    # 会显示成 `$0.0000`，读起来就是"没花钱"——而钱是实实在在花掉了的
    missing = snapshot.cost_missing_attempts
    caveat = f"；其中 {missing} 次报不出成本，这个金额是不全的" if missing else ""
    print(
        f"  成本 / token  ${float(snapshot.total_cost_usd):.4f} / {snapshot.total_tokens}"
        f"（累计全部 attempt，C-56{caveat}）"
    )
    print(f"  makespan      {_ms(snapshot.makespan_ms)}")
    print(f"  并发设置      agent={run.agent_concurrency}  sandbox={run.sandbox_concurrency}")

    jobs = list(
        session.execute(
            sa.select(JobQueue.state, sa.func.count())
            .where(JobQueue.payload[RUN_ID_KEY].astext == str(run_id))
            .group_by(JobQueue.state)
            .order_by(JobQueue.state)
        ).all()
    )
    if jobs:
        print("  作业          " + "  ".join(f"{state.value}={count}" for state, count in jobs))
    return 0


def _rate(value: Decimal | None) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _ms(value: int | None) -> str:
    if value is None:
        return "—"
    seconds = value / 1000
    return f"{seconds:.1f} 秒" if seconds < 120 else f"{seconds / 60:.1f} 分钟"


# ── cancel ──────────────────────────────────────────────────


def cmd_cancel(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        try:
            summary = cancel_run(session, args.run)
        except OrchestrationError as exc:
            print(f"取消不了：{exc}")
            return 1

    if summary.already_cancelled:
        print(f"实验 #{args.run} 本来就是已取消状态")
        return 0
    print(f"实验 #{args.run} 已标为 CANCELLED")
    print(f"  还没开跑的 {summary.dropped_jobs} 条作业已经掐掉")
    if summary.in_flight_jobs:
        print(
            f"  还有 {summary.in_flight_jobs} 条正在跑：Worker 最多 "
            f"{settings.cancel_poll_s:.0f} 秒后发现，杀掉容器并记成 CANCELLED"
        )
    return 0


# ── retry-failed ────────────────────────────────────────────


def cmd_retry_failed(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        try:
            summary = retry_failed(session, args.run)
        except OrchestrationError as exc:
            print(f"补不了：{exc}")
            return 1

    print(f"实验 #{args.run}：")
    print(f"  补投作业      {len(summary.requeued)} 题 {list(summary.requeued) or ''}")
    print(f"  已有结论不碰  {summary.already_decided} 题（协议 C-25）")
    print(f"  还在跑        {summary.still_running} 题")
    if summary.at_attempt_cap:
        print(f"  到 4 次上限    {list(summary.at_attempt_cap)} 题，不再补（协议 C-71）")
    if summary.requeued:
        print("起 Worker 来跑：python -m app.worker")
    return 0


# ── concurrency ─────────────────────────────────────────────


def cmd_concurrency(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        points = concurrency_mod.series_for(session, args.run)

    if not points:
        print("这些实验里还没有跑完的执行，画不出曲线")
        return 1

    print(f"变化点 {len(points)} 个，覆盖 {points[0].at:%H:%M:%S} → {points[-1].at:%H:%M:%S}")
    print(f"{'曲线':<12} {'峰值':>5} {'P50':>5}   含义")
    meanings = {
        "in_flight": "同时在途的题数（对外声明的并行度，§4.6）",
        "agent": "同时在跑的被测 AI 数",
        "sandbox": "同时在跑的测试容器数",
    }
    for curve in concurrency_mod.CURVES:
        summary = concurrency_mod.summarize(points, curve)
        print(f"{curve:<12} {summary.peak:>5} {summary.p50:>5}   {meanings[curve]}")

    if args.csv:
        path = Path(args.csv)
        path.write_text(concurrency_mod.to_csv(points), encoding="utf-8")
        print(f"CSV 已写到 {path}")
    return 0


# ── timing ──────────────────────────────────────────────────


def cmd_timing(args: argparse.Namespace) -> int:
    """阶段耗时 + 把实测的 A / S 回代进 §18.2 的 makespan 模型（E9-T1）。

    两件事一起做是有意的：量到的 `A` 只有放进模型里才回答得了"300 次压不压得进
    6 小时"，而分开两条命令的话，中间那一步手算，就又回到 §18.2 那张手算表的老路上。
    """
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        attempts = timing_mod.load_attempts(session, args.run)

    if not attempts:
        print("这些实验里一次执行都没有")
        return 1

    timings = timing_mod.summarize(attempts)
    actual_minutes = timing_mod.actual_makespan_minutes(attempts)

    print(f"实验 {', '.join('#' + str(r) for r in args.run)}  共 {len(attempts)} 次执行")
    if actual_minutes is not None:
        print(f"实测 makespan  {actual_minutes:.1f} 分钟（最早开始 → 最后结束，跨全部实验）")
    print()

    for item in timings:
        _print_agent_timing(item)

    _print_projection(
        timings,
        all_attempts=attempts,
        actual_minutes=actual_minutes,
        args=args,
        settings=settings,
    )

    if args.csv:
        path = Path(args.csv)
        path.write_text(timing_mod.to_csv(attempts), encoding="utf-8")
        print(f"\n逐次执行的 CSV 已写到 {path}")
    return 0


def _print_agent_timing(item: timing_mod.AgentTiming) -> None:
    print(f"── {item.agent_name}  {item.attempts} 次执行 " + "─" * 30)
    print(
        f"   超时 {item.timeouts} 次（{item.timeout_rate * 100:.1f}%）"
        "  ← 一次超时往 A 里塞满一个 agent_timeout_s"
    )
    print(f"   {'阶段':<9} {'样本':>4} {'均值':>8} {'P50':>8} {'P95':>8} {'最大':>8}   含义")
    for stage in timing_mod.STAGES:
        stats = item.stats.get(stage)
        if stats is None:
            continue
        print(
            f"   {stage:<9} {stats.samples:>4} {_secs(stats.mean_s):>8} {_secs(stats.p50_s):>8} "
            f"{_secs(stats.p95_s):>8} {_secs(stats.max_s):>8}   "
            f"{timing_mod.STAGE_MEANINGS[stage]}"
        )
    print()


def _print_projection(
    timings: Sequence[timing_mod.AgentTiming],
    *,
    all_attempts: Sequence[timing_mod.Attempt],
    actual_minutes: float | None,
    args: argparse.Namespace,
    settings: Settings,
) -> None:
    """把实测值回代进 §18.2 的模型，然后查一次降级表。

    `A` 取**各 Agent 里最大的那个**，不取总平均。理由：MET-02 要的是 100 题 ×
    3 Agent 全部跑完，最慢的那个 Agent 决定什么时候能收工；而且这张 pilot 只有
    2 个真实 Agent，第三个的 A 还不知道，拿已知里最慢的当上界才是该用的方向。
    """
    real = [t for t in timings if t.agent_minutes > 0]
    if not real:
        print("没有一个 Agent 的阶段耗时大于 0 —— 这批全是 Oracle / Noop，投影不了 A")
        return

    slowest = max(real, key=lambda t: t.agent_minutes)
    agent_minutes = slowest.agent_minutes
    other_minutes = max(t.other_minutes for t in real)
    # 最慢的那一道题：反算损耗系数时要用（一道题拆不开并行，见 makespan 模块开头）
    longest_task_minutes = max(
        (stats.max_s / 60 for t in real if (stats := t.stats.get("total")) is not None),
        default=0.0,
    )

    agent_limit = args.agent_limit or settings.agent_concurrency
    sandbox_limit = args.sandbox_limit or settings.sandbox_concurrency
    worker_slots = args.worker_slots or settings.worker_slots

    # 先拿这批运行自己反算损耗系数，再用它去推 N 次。不给 --overhead 就用实测值。
    #
    # 本批的理论下限按**真实工时求和**算，不按"均值 × 次数"：这一批是混合负载
    # （Oracle 的 Agent 阶段是 0，两个真实 Agent 又不一样快），而 `agent_minutes`
    # 取的是最慢那个。拿最慢的均值乘总次数，分子比这批真干的活还多，
    # 反算出来是负数、被钳到 0，看着像零开销（`total_stage_minutes` 的注释里有实测数）。
    batch = MakespanInputs(
        runs=len(all_attempts),
        agent_minutes=agent_minutes,
        other_minutes=other_minutes,
        agent_limit=agent_limit,
        sandbox_limit=sandbox_limit,
        worker_slots=worker_slots,
        overhead_ratio=0.0,
        longest_task_minutes=longest_task_minutes,
    )
    batch_theoretical = max(
        timing_mod.total_stage_minutes(all_attempts, "agent") / batch.effective_agent_limit,
        timing_mod.total_stage_minutes(all_attempts, "other") / batch.effective_sandbox_limit,
        longest_task_minutes,
    )
    if args.overhead is not None:
        overhead = args.overhead
        overhead_note = "命令行给的"
    elif actual_minutes is None:
        overhead = DEFAULT_OVERHEAD_RATIO
        overhead_note = "算不出实测值，退回 §18.2 的假设"
    elif not batch.saturates_slots:
        # 槽位没填满的批次根本没排过队，反算出来的数量不出调度损耗（见 makespan 模块开头）
        overhead = DEFAULT_OVERHEAD_RATIO
        overhead_note = (
            f"这批只有 {len(all_attempts)} 次运行、{batch.effective_agent_limit} 个槽位，"
            f"填不满（要 ≥{2 * batch.effective_agent_limit} 次），"
            "反算不出调度损耗，退回 §18.2 的假设"
        )
    else:
        overhead = measured_overhead(
            actual_minutes=actual_minutes, theoretical_minutes=batch_theoretical
        )
        overhead_note = f"本批实测反算（{actual_minutes:.1f} / {batch_theoretical:.1f} 分钟）"

    inputs = MakespanInputs(
        runs=args.project_n,
        agent_minutes=agent_minutes,
        other_minutes=other_minutes,
        agent_limit=agent_limit,
        sandbox_limit=sandbox_limit,
        worker_slots=worker_slots,
        overhead_ratio=overhead,
        longest_task_minutes=longest_task_minutes,
    )
    projection = project(inputs)

    print("══ 回代 §18.2 的 makespan 模型 " + "═" * 28)
    print(f"   A  {agent_minutes:.2f} 分钟   取最慢的 Agent（{slowest.agent_name}），不取总平均")
    print(f"   S  {other_minutes:.2f} 分钟   单题总计减掉 Agent 阶段")
    print(f"   最慢一道题  {longest_task_minutes:.2f} 分钟   一道题拆不开并行，它本身是下限")
    print(f"   损耗系数  {overhead * 100:.1f}%   {overhead_note}")
    print(
        f"   并发  agent={agent_limit} sandbox={sandbox_limit} slots={worker_slots}"
        f"  →  有效 P_agent={inputs.effective_agent_limit}"
        f" P_sandbox={inputs.effective_sandbox_limit}"
    )
    if inputs.effective_agent_limit < agent_limit:
        print(
            f"         ↑ agent_concurrency={agent_limit} 被槽位封到"
            f" {inputs.effective_agent_limit} —— 一道题同一时刻只占一个槽位"
        )
    print()
    print(f"   投影 N={inputs.runs}（MET-02 的口径是 100 题 × 3 Agent）")
    print(f"     Agent 侧    {projection.agent_side_minutes:>7.1f} 分钟")
    print(f"     Sandbox 侧  {projection.sandbox_side_minutes:>7.1f} 分钟")
    print(f"     单题下限    {projection.single_task_floor_minutes:>7.1f} 分钟")
    print(f"     瓶颈        {projection.bottleneck}")
    print(
        f"     投影 makespan {projection.projected_minutes:.1f} 分钟 = "
        f"{projection.projected_hours:.2f} 小时"
    )
    verdict = "达标" if projection.fits() else "超线"
    headroom = projection.headroom_minutes()
    print(f"     MET-02（≤{TARGET_HOURS:g} 小时）{verdict}，余量 {headroom:+.0f} 分钟")
    ceiling = max_agent_minutes(inputs)
    print(f"     A 还能涨到 {ceiling:.2f} 分钟 —— 再多就压不进 {TARGET_HOURS:g} 小时了")
    band = projection.band()
    print(f"\n   降级表（跑之前定的）命中 [{band.key}]：{band.action}")


def _secs(value: float) -> str:
    return f"{value:.1f}s" if value < 120 else f"{value / 60:.1f}m"


# ── manifest ────────────────────────────────────────────────


def cmd_manifest(args: argparse.Namespace) -> int:
    """看一次运行的 manifest，或者比两次运行的 manifest。

    给两个 `--run` 就是比对。返回码有意义：必须相同的字段有差异时返回 1，
    好让它能直接当脚本里的断言用。
    """
    factory = create_session_factory(create_db_engine(get_settings().database_url))
    with session_scope(factory) as session:
        loaded: list[tuple[int, dict[str, object]]] = []
        for run_id in args.run:
            run = session.get(EvaluationRun, run_id)
            if run is None:
                print(f"找不到实验 #{run_id}")
                return 1
            if not run.manifest:
                print(f"实验 #{run_id} 没有 manifest —— 它是 E5-T4 之前建的，那时这一列是空的")
                return 1
            loaded.append((run_id, dict(run.manifest)))

        if len(loaded) == 1:
            run_id, manifest = loaded[0]
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                _print_manifest(run_id, manifest)
            return 0
        if len(loaded) != 2:
            print(f"比对只能给两个 --run，收到 {len(loaded)} 个")
            return 1
        (left_id, left), (right_id, right) = loaded
        return _print_diff(left_id, right_id, diff_manifests(left, right))


def _print_manifest(run_id: int, manifest: dict[str, object]) -> None:
    dataset = block(manifest, "dataset")
    agent = block(manifest, "agent")
    images = block(manifest, "images")
    limits = block(manifest, "limits")
    host = block(manifest, "host")

    print(f"实验 #{run_id} 的运行 manifest（结构版本 {manifest.get('manifest_version')}）\n")
    print(f"  协议版本    {manifest.get('protocol_version')}")
    print(f"  harness     {manifest.get('harness_git_sha')}")
    if manifest.get("dirty"):
        print("              ⚠ dirty=true —— 工作区带未提交改动（协议 C-28：不得进排行榜）")
    print(f"  数据集      {dataset.get('slug')}@{dataset.get('version')}")
    print(f"              摘要 {manifest.get(MANIFEST_DIGEST_KEY)}")
    selected = dataset.get("selected_task_ids")
    if selected:
        count = dataset.get("selected_task_count")
        print(f"              只投了 {count} 道：{'、'.join(selected)}")
    else:
        print(f"              整份快照 {dataset.get('snapshot_task_count')} 道全投")
    print(f"  参赛者      {agent.get('label')}（{agent.get('name')} × {agent.get('model_name')}）")
    print(f"              版本 {agent.get('agent_version')} · 参数哈希 {agent.get('config_hash')}")
    print(f"  镜像        {len(images)} 个环境（协议 C-36：按 digest 引用）")
    for env_id, ref in sorted(images.items()):
        digest = (ref or {}).get("digest") if isinstance(ref, dict) else None
        print(f"                {env_id:<32} {digest or '（还没建过，只能按 tag 起）'}")
    print(f"  限额        {limits}")
    if manifest.get("gate_for"):
        print(f"  门禁凭据    {manifest['gate_for']}")
    if manifest.get("replay_of"):
        print(f"  重放自      实验 #{manifest['replay_of']}")
    print(f"\n  以下允许两次运行不同（{'、'.join(sorted(VOLATILE_KEYS))}）：")
    print(f"    建于      {manifest.get('created_at')}")
    print(
        f"    机器      docker {host.get('docker_version')} · 内核 {host.get('kernel_version')}"
        f" · {host.get('cpu_count')} 核 · {host.get('memory_mb')} MiB"
    )


def _print_diff(left_id: int, right_id: int, result: ManifestDiff) -> int:
    print(f"实验 #{left_id} ↔ #{right_id} 的 manifest 差异\n")
    if result.pinned:
        print(f"必须相同却不同的字段（{len(result.pinned)} 处）——**这两次运行不等价**：")
        for item in result.pinned:
            print(f"  {item.path}")
            print(f"    #{left_id}  {item.left}")
            print(f"    #{right_id}  {item.right}")
    else:
        print("必须相同的字段：全部一致 ✅")
    print()
    if result.volatile:
        print(f"允许不同的字段（{len(result.volatile)} 处，不影响等价判断）：")
        for item in result.volatile:
            print(f"  {item.path}")
            print(f"    #{left_id}  {item.left}")
            print(f"    #{right_id}  {item.right}")
    else:
        print("允许不同的字段：也全部一致")
    if result.pinned:
        return 1
    print("\n→ 两次运行的输入条件等价。")
    print("  注意这不等于“两次结果会一样”：协议 C-73 写的是测试执行的可复现性")
    print("  是目标不是保证，逐实例一致率是 MET-01 的口径（E10-T5）。")
    return 0


# ── replay ──────────────────────────────────────────────────


def cmd_replay(args: argparse.Namespace) -> int:
    """按某次运行的 manifest 重建一次等价运行（E5-T4 的 AC："由 manifest 可重建"）。

    六项前置校验全过才建，任何一项不过都点名说差在哪：

    1. 协议版本 —— 协议改版后旧结果不重算（C-52），拿新协议重放不是同一件事
    2. 数据集快照摘要 —— 有人动过题目清单就不是同一批题了
    3. 题目内容漂移 —— 清单没动但题被改过，同样不是同一件事（第 2 条抓不到，见下）
    4. 题目清单 —— 原来只投了子集的话，这一批题必须还在
    5. 镜像 digest —— 记的那几个本地还在不在（被 GC 掉就重建不了）
    6. Agent 参数哈希 —— 参数改过就不是同一个参赛者

    全过之后**重新采集一遍凭证**再和老 manifest 逐字段比，而不是把老 manifest
    抄一份。抄一份的话，"世界还是不是当初那样"这个问题根本没被问过。
    """
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with session_scope(factory) as session:
        source = session.get(EvaluationRun, args.run)
        if source is None:
            print(f"找不到实验 #{args.run}")
            return 1
        old = dict(source.manifest or {})
        if not old:
            print(f"实验 #{args.run} 没有 manifest —— 它是 E5-T4 之前建的，重建不了。")
            print(
                "那时 evaluation_runs.manifest 这一列一直是空的，跑的哪版代码、"
                "哪个镜像都没记，事后补不出来。"
            )
            return 1

        # ① 协议版本
        if old.get("protocol_version") != PROTOCOL_VERSION:
            print(
                f"协议版本对不上：实验 #{args.run} 依据的是 {old.get('protocol_version')}，"
                f"当前代码是 {PROTOCOL_VERSION}。"
            )
            print("协议 C-52：协议改版后旧结果不重算。要复现那次，得先 checkout 到那一版协议。")
            return 1

        # ② 数据集还在不在、摘要对不对
        dataset = session.get(BenchmarkSet, source.benchmark_set_id)
        if dataset is None:
            print(f"实验 #{args.run} 用的数据集版本（id={source.benchmark_set_id}）已经不在库里了")
            return 1
        all_rows = items_of(session, dataset.id)
        digest = snapshot_digest(all_rows)
        if digest != old.get(MANIFEST_DIGEST_KEY):
            print(f"数据集摘要对不上 —— {dataset.slug}@{dataset.version} 的题被改过了。")
            print(f"  实验 #{args.run} 记的  {old.get(MANIFEST_DIGEST_KEY)}")
            print(f"  现在算出来的        {digest}")
            print(
                "查是哪几道题变了：python -m cli.dataset verify --slug "
                f"{dataset.slug} --version {dataset.version}"
            )
            return 1

        # ②b 题目内容有没有漂移。
        #
        # 上面那个摘要比的是**冻住的**哈希（`items_of()` 读的是
        # `benchmark_set_items.task_content_hash`），所以它只抓得到"有人动了题目清单"。
        # 而 Worker 跑题读的是 `benchmark_tasks.raw_definition` —— **活的那一份**。
        # 题被改过时，摘要照旧对得上，跑的却是另一份内容。这一步专门抓这个。
        moved = drift(session, dataset.id)
        if moved.changed or moved.quarantined or moved.missing:
            print(f"{dataset.slug}@{dataset.version} 的题目和当初冻的那一版对不上了：")
            if moved.changed:
                print(f"  内容变了 {len(moved.changed)} 道：{'、'.join(moved.changed)}")
            if moved.quarantined:
                print(f"  被隔离了 {len(moved.quarantined)} 道：{'、'.join(moved.quarantined)}")
            if moved.missing:
                print(f"  题不见了 {len(moved.missing)} 道：{'、'.join(moved.missing)}")
            print("\n快照冻的是当初那份内容的哈希，而跑题读的是库里现在这一份。")
            print(
                "两者不一致时重放出来的不是同一件事。细账：python -m cli.dataset verify "
                f"--slug {dataset.slug} --version {dataset.version}"
            )
            return 1

        # ③ 题目清单（原来只投了子集的话，那几道题必须还在）
        rows = all_rows
        wanted = block(old, "dataset").get("selected_task_ids")
        if wanted:
            have = {row.task_id for row in all_rows}
            gone = sorted(set(wanted) - have)
            if gone:
                print(
                    f"原来投的 {len(wanted)} 道题里有 {len(gone)} 道现在不在快照里："
                    f"{'、'.join(gone)}"
                )
                return 1
            rows = [row for row in all_rows if row.task_id in set(wanted)]
        task_ids = [row.benchmark_task_id for row in rows]

        # ④ 镜像还在不在（协议 C-36：按 digest 引用，digest 没了就重建不了环境）
        if not _images_present(old, run_id=args.run):
            return 1

        # ⑤ Agent 参数（config_hash 由下面的 manifest diff 逐字段比，这里先确认配置还在）
        if session.get(AgentConfig, source.agent_config_id) is None:
            print(f"实验 #{args.run} 用的 Agent 配置（id={source.agent_config_id}）已经不在库里了")
            return 1

        try:
            provenance = collect_provenance(
                session,
                benchmark_set_id=dataset.id,
                snapshot_digest=digest,
                agent_config_id=source.agent_config_id,
                task_ids=task_ids,
                agent_concurrency=source.agent_concurrency,
                sandbox_concurrency=source.sandbox_concurrency,
                job_max_attempts=int(
                    block(old, "limits").get("job_max_attempts", settings.job_max_attempts)
                ),
                allow_dirty=args.allow_dirty,
                replay_of=source.id,
            )
        except ProvenanceError as exc:
            print(exc)
            return 1

        # 重新采集的凭证 vs 老 manifest：必须相同的字段有差异就不建
        result = diff_manifests(old, build_manifest(provenance))
        drifted = _code_drift_only(result)
        if result.pinned and not (drifted and args.allow_code_drift):
            print(f"重建不了实验 #{args.run} —— 输入条件已经变了：\n")
            for item in result.pinned:
                print(f"  {item.path}")
                print(f"    当初  {item.left}")
                print(f"    现在  {item.right}")
            if drifted:
                print(
                    f"\n只差在代码版本上。要在当初那版代码上重放：git checkout "
                    f"{old.get('harness_git_sha')}"
                )
                print("要用现在这版代码重放（结果会是另一个口径）：加 --allow-code-drift")
            return 1

        name = args.name or f"重放 #{source.id} · {source.name}"
        try:
            runs = create_runs(
                session, name=name, task_ids=task_ids, provenance=provenance, rounds=args.rounds
            )
        except OrchestrationError as exc:
            print(f"建不了：{exc}")
            return 1
        created = [(run.id, run.name) for run in runs]
        dirty = provenance.dirty

    print(
        f"按实验 #{args.run} 的 manifest 重建了 {len(created)} 次等价运行"
        f"（各 {len(task_ids)} 道题）："
    )
    for run_id, run_name in created:
        print(f"  实验 #{run_id}（{run_name}）")
    if result.pinned:
        print(
            "\n⚠ 代码版本和当初不同（--allow-code-drift 放行的），"
            "新运行的 manifest 如实记的是现在这版"
        )
    _warn_if_dirty(dirty)
    print("\n起 Worker 来跑：python -m app.worker")
    print(f"跑完比一比：python -m cli.experiment manifest --run {args.run} --run {created[0][0]}")
    return 0


def _images_present(manifest: dict[str, object], *, run_id: int) -> bool:
    """manifest 里记的镜像 digest 本地还在不在。

    docker 连不上时**只警告不拒绝**：`replay` 干的事只是建实验投队列，
    真正要 docker 的是 Worker。连不上就是"查不了"，不是"不在"。
    """
    images = block(manifest, "images")
    wanted = {
        env_id: pinned_image_ref(manifest, env_id)
        for env_id in images
        if pinned_image_ref(manifest, env_id)
    }
    if not wanted:
        return True
    missing: list[tuple[str, str]] = []
    for env_id, reference in sorted(wanted.items()):
        assert reference is not None
        try:
            inspect_image(reference)
        except ImageNotFoundError:
            missing.append((env_id, reference))
        except SandboxError as exc:
            print(f"⚠ 查不了镜像（{exc}），跳过这一项检查。Worker 起容器时会再报一次。")
            return True
    if missing:
        print(f"实验 #{run_id} 记的镜像已经不在本地了，重建不了环境（协议 C-36）：")
        for env_id, reference in missing:
            print(f"  {env_id:<32} {reference}")
        print(
            "\n多半是被 `cli.images gc` 回收了，或者环境重建过。"
            "重建镜像只能保证配方一样，保证不了内容一样。"
        )
        return False
    return True


def _code_drift_only(result: ManifestDiff) -> bool:
    """差异是不是只出在 harness 的代码版本上。"""
    return bool(result.pinned) and all(
        item.path in ("harness_git_sha", "dirty") for item in result.pinned
    )


# ── 命令行 ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.experiment", description="实验编排")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="建实验并把题投进队列")
    p_start.add_argument("--agent", default="oracle", help="Agent 名字，默认 oracle")
    p_start.add_argument("--set", default=GOLDEN_SET_SLUG, help="数据集 slug，默认 golden")
    p_start.add_argument("--version", help="数据集版本，默认取最新已发布的那一版")
    p_start.add_argument("--name", default="adhoc", help="实验名")
    p_start.add_argument(
        "--task", action="append", help="只投这几道题（task_id，可重复给）。不给就投全部"
    )
    p_start.add_argument(
        "--rounds", type=int, default=1, help="跑几轮（每轮一个 EvaluationRun，协议 C-55）"
    )
    p_start.add_argument("--agent-concurrency", type=int, help="记进实验的 Agent 并发，默认取配置")
    p_start.add_argument("--sandbox-concurrency", type=int, help="记进实验的沙箱并发，默认取配置")
    p_start.add_argument(
        "--allow-dirty",
        action="store_true",
        help="工作区不干净也建（协议 C-28：结果标 dirty=true，不得进排行榜）",
    )
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status", help="看实验进度")
    p_status.add_argument("--run", type=int, help="只看这一个实验的细账")
    p_status.add_argument("--limit", type=int, default=20, help="列表最多显示几条")
    p_status.set_defaults(func=cmd_status)

    p_cancel = sub.add_parser("cancel", help="取消一次实验")
    p_cancel.add_argument("--run", type=int, required=True)
    p_cancel.set_defaults(func=cmd_cancel)

    p_retry = sub.add_parser("retry-failed", help="把没有结论的题补跑（只补洞，见协议 C-25/C-55）")
    p_retry.add_argument("--run", type=int, required=True)
    p_retry.set_defaults(func=cmd_retry_failed)

    p_manifest = sub.add_parser("manifest", help="看运行 manifest；给两个 --run 就是比对")
    p_manifest.add_argument("--run", type=int, action="append", required=True, help="实验号")
    p_manifest.add_argument("--json", action="store_true", help="输出原始 JSON（只看一个时有效）")
    p_manifest.set_defaults(func=cmd_manifest)

    p_replay = sub.add_parser("replay", help="按某次运行的 manifest 重建一次等价运行")
    p_replay.add_argument("--run", type=int, required=True, help="要重放哪一次")
    p_replay.add_argument("--name", help="新实验的名字，默认 '重放 #N · 原名'")
    p_replay.add_argument("--rounds", type=int, default=1, help="重放几轮（协议 C-55）")
    p_replay.add_argument(
        "--allow-dirty", action="store_true", help="工作区不干净也建（协议 C-28）"
    )
    p_replay.add_argument(
        "--allow-code-drift",
        action="store_true",
        help="harness 代码版本和当初不同也建（新运行的 manifest 如实记现在这版）",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_timing = sub.add_parser("timing", help="阶段耗时 P50/P95/最大值 + makespan 投影")
    p_timing.add_argument(
        "--run", type=int, action="append", required=True, help="实验号，可重复给"
    )
    p_timing.add_argument("--csv", help="把逐次执行的阶段耗时写到这个文件")
    p_timing.add_argument(
        "--project-n", type=int, default=TARGET_RUNS, help=f"投影多少次运行，默认 {TARGET_RUNS}"
    )
    p_timing.add_argument(
        "--overhead", type=float, help="调度损耗系数（0.25 = 25%%）。不给就用本批实测反算"
    )
    p_timing.add_argument("--agent-limit", type=int, help="投影用的 AGENT_CONCURRENCY，默认取配置")
    p_timing.add_argument(
        "--sandbox-limit", type=int, help="投影用的 SANDBOX_CONCURRENCY，默认取配置"
    )
    p_timing.add_argument("--worker-slots", type=int, help="投影用的 WORKER_SLOTS，默认取配置")
    p_timing.set_defaults(func=cmd_timing)

    p_conc = sub.add_parser("concurrency", help="导出有效并发时间序列")
    p_conc.add_argument("--run", type=int, action="append", required=True, help="实验号，可重复给")
    p_conc.add_argument("--csv", help="把时序写到这个文件")
    p_conc.set_defaults(func=cmd_concurrency)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
