"""实验的命令行：建、看、取消、补跑、导并发时序（E5-T2）。

    python -m cli.experiment start --agent oracle --rounds 3   # 建 3 个实验并投作业
    python -m cli.experiment status                            # 看所有实验
    python -m cli.experiment status --run 12                   # 看一个实验的细账
    python -m cli.experiment cancel --run 12                   # 取消
    python -m cli.experiment retry-failed --run 12             # 把没结论的题补跑
    python -m cli.experiment concurrency --run 12 --run 13     # 有效并发时序

配合 `python -m app.worker` 用：一个终端起 Worker，另一个终端在这里操作。

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
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.evaluation import concurrency as concurrency_mod
from app.evaluation import progress as progress_mod
from app.evaluation.orchestrator import (
    OrchestrationError,
    cancel_run,
    create_runs,
    retry_failed,
)
from app.infrastructure.config import get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun
from app.infrastructure.models.job import JobQueue
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

        dataset = session.execute(
            sa.select(BenchmarkSet).where(BenchmarkSet.slug == args.set)
        ).scalar_one_or_none()
        if dataset is None:
            print(f"找不到数据集 {args.set}，先跑 `python -m cli.queue seed-golden`")
            return 1

        query = sa.select(BenchmarkTask.id).order_by(BenchmarkTask.id)
        if args.task:
            query = query.where(BenchmarkTask.task_id.in_(args.task))
        task_ids = list(session.execute(query).scalars())
        if not task_ids:
            print("没选中任何题目，先跑 `python -m cli.queue seed-golden`（或检查 --task 拼写）")
            return 1

        try:
            runs = create_runs(
                session,
                name=args.name,
                benchmark_set_id=dataset.id,
                agent_config_id=config.id,
                task_ids=task_ids,
                agent_concurrency=args.agent_concurrency or settings.agent_concurrency,
                sandbox_concurrency=args.sandbox_concurrency or settings.sandbox_concurrency,
                rounds=args.rounds,
                job_max_attempts=settings.job_max_attempts,
            )
        except OrchestrationError as exc:
            print(f"建不了：{exc}")
            return 1
        created = [(run.id, run.name) for run in runs]

    for run_id, name in created:
        print(f"实验 #{run_id}（{name}）已建，投了 {len(task_ids)} 条 EVAL_TASK 作业")
    print(f"共 {len(created)} 轮 × {len(task_ids)} 题 = {len(created) * len(task_ids)} 条作业")
    print("起 Worker 来跑：python -m app.worker")
    return 0


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
    print(
        f"  成本 / token  ${float(snapshot.total_cost_usd):.4f} / {snapshot.total_tokens}"
        "（累计全部 attempt，C-56）"
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


# ── 命令行 ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.experiment", description="实验编排")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="建实验并把题投进队列")
    p_start.add_argument("--agent", default="oracle", help="Agent 名字，默认 oracle")
    p_start.add_argument("--set", default=GOLDEN_SET_SLUG, help="数据集 slug，默认 golden")
    p_start.add_argument("--name", default="adhoc", help="实验名")
    p_start.add_argument(
        "--task", action="append", help="只投这几道题（task_id，可重复给）。不给就投全部"
    )
    p_start.add_argument(
        "--rounds", type=int, default=1, help="跑几轮（每轮一个 EvaluationRun，协议 C-55）"
    )
    p_start.add_argument("--agent-concurrency", type=int, help="记进实验的 Agent 并发，默认取配置")
    p_start.add_argument("--sandbox-concurrency", type=int, help="记进实验的沙箱并发，默认取配置")
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
