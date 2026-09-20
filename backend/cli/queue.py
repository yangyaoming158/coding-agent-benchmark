"""作业队列的命令行：投作业、看状态（E5-T1）。

    python -m cli.queue seed-golden              # 把 Golden 题写进库（开发用）
    python -m cli.queue enqueue --agent oracle   # 建一次实验，把题全投进队列
    python -m cli.queue status                   # 看队列现在什么样

配合 `python -m app.worker` 用：一个终端起 Worker，另一个终端投作业。

## `seed-golden` 不是题目验证流水线

它只做一件事：把 `datasets/golden/*.json` 原样写成 `benchmark_tasks` 行，
`validation_state` 留在 `DISCOVERED`。**没有**跑基线测试、没有验 F2P 真的会失败、
没有算 P2P 抽样——那八步是 E1-T3 的活（`03-benchmark-spec.md` §7.3）。

Golden 题是我们自己造出来当测试基石的，本来就已知是好的，所以开发期这样够用。
真实题目**必须**走 E1-T3，否则会有坏题混进数据集，Oracle 哨兵就不再是 100%。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.dataset import DatasetError, items_of, resolve_set, snapshot_digest
from app.benchmark.schema import TaskDefinition
from app.domain.enums import JobState, TaskValidationState
from app.evaluation.agent_configs import AgentConfigError, resolve_agent_config
from app.evaluation.manifest import ProvenanceError, collect_provenance
from app.evaluation.orchestrator import OrchestrationError, create_runs
from app.infrastructure.config import REPO_ROOT, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from app.infrastructure.models.job import JobQueue

GOLDEN_ROOT = REPO_ROOT / "datasets" / "golden"
GOLDEN_SET_SLUG = "golden"


# ── seed-golden ─────────────────────────────────────────────


def load_environments() -> dict[str, dict[str, Any]]:
    """`datasets/golden/environments/*.json` → `environment_id` → 规格。"""
    envs: dict[str, dict[str, Any]] = {}
    for path in sorted((GOLDEN_ROOT / "environments").glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        envs[str(spec["environment_id"])] = spec
    return envs


def upsert_repository(session: Session, task: TaskDefinition) -> Repository:
    repo = session.execute(
        sa.select(Repository).where(Repository.full_name == task.repo_name)
    ).scalar_one_or_none()
    if repo is None:
        repo = Repository(full_name=task.repo_name, url=task.repo_url, language=task.language)
        session.add(repo)
        session.flush()
    return repo


def upsert_environment(
    session: Session, task: TaskDefinition, repo: Repository, spec: dict[str, Any]
) -> EnvironmentSpec:
    env = session.execute(
        sa.select(EnvironmentSpec).where(EnvironmentSpec.environment_id == task.environment_id)
    ).scalar_one_or_none()
    if env is None:
        env = EnvironmentSpec(
            environment_id=task.environment_id,
            repository_id=repo.id,
            python_version=str(spec.get("python_version", "3.11")),
            install_command=task.install_command,
            pre_test_command=task.pre_test_command,
            test_command=task.test_command,
            test_framework=task.test_framework,
            test_report_path=task.test_report_path,
            extra_protected_paths=list(spec.get("extra_protected_paths", [])),
            image_tag=spec.get("image_tag"),
        )
        session.add(env)
        session.flush()
    return env


def upsert_task(
    session: Session,
    task: TaskDefinition,
    environments: dict[str, Any],
    *,
    patch_uri_scheme: str = "golden",
) -> bool:
    """写一道题，返回是不是新建的。

    `raw_definition` 存**完整的题目 JSON**，Worker 跑的时候从它还原 `TaskDefinition`。
    那十几个列只是给 SQL 查询用的投影——`test_patch` 和 `gold_patch` 根本不在列里。

    `patch_uri_scheme` 只影响那两个 uri 列的写法，用来一眼看出补丁是哪来的：
    Golden 题是 `make golden` 生成的，挖掘来的题是 E1-T5 从真实 PR 劈出来的。
    """
    spec = environments.get(task.environment_id)
    if spec is None:
        raise SystemExit(f"题目 {task.task_id} 引用的环境 {task.environment_id} 找不到规格文件")

    repo = upsert_repository(session, task)
    env = upsert_environment(session, task, repo, spec)

    row = session.execute(
        sa.select(BenchmarkTask).where(BenchmarkTask.task_id == task.task_id)
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = BenchmarkTask(task_id=task.task_id, repository_id=repo.id)
        session.add(row)

    row.environment_spec_id = env.id
    row.base_commit = task.base_commit
    row.issue_title = task.issue_title
    row.issue_body = task.issue_body
    row.issue_language = task.issue_language
    row.source_issue_url = task.source_issue_url
    row.source_pr_url = task.source_pr_url
    row.fail_to_pass = list(task.fail_to_pass)
    row.pass_to_pass = list(task.pass_to_pass)
    # 补丁正文在 raw_definition 里，这两个 uri 列留给 E1-T3 落制品之后回填
    row.test_patch_uri = f"{patch_uri_scheme}://{task.task_id}/test.patch"
    row.test_patch_paths = list(task.test_patch_paths)
    row.gold_patch_uri = f"{patch_uri_scheme}://{task.task_id}/gold.patch"
    row.difficulty = task.difficulty
    row.tags = list(task.tags)
    row.agent_timeout_s = task.agent_timeout_s
    row.test_timeout_s = task.test_timeout_s
    row.sandbox_cpu = task.sandbox_cpu  # type: ignore[assignment]
    row.sandbox_memory_mb = task.sandbox_memory_mb
    row.sandbox_pids_limit = task.sandbox_pids_limit
    if created:
        # 没跑过验证流水线，状态就只能是 DISCOVERED。写成 VALID 是在撒谎，
        # 而下游（E8 的数据集发布）会拿这个状态当发布门槛。
        #
        # **只在新建时写。** 每次 upsert 都重置的话，重跑一遍入库就会把已经验过的题
        # 打回 DISCOVERED —— 不报错，只是那几十个容器白跑了，而且下一次
        # `make validate-tasks` 才看得出来。
        row.validation_state = TaskValidationState.DISCOVERED
    row.content_hash = (task.content_hash or "").removeprefix("sha256:") or "0" * 64
    row.raw_definition = json.loads(task.model_dump_json())
    session.flush()
    return created


def cmd_seed_golden(_args: argparse.Namespace) -> int:
    environments = load_environments()
    files = sorted(GOLDEN_ROOT.glob("*.json"))
    if not files:
        print(f"{GOLDEN_ROOT} 下没有题目文件，先跑 `make golden`")
        return 1

    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        dataset = session.execute(
            sa.select(BenchmarkSet).where(BenchmarkSet.slug == GOLDEN_SET_SLUG)
        ).scalar_one_or_none()
        if dataset is None:
            dataset = BenchmarkSet(slug=GOLDEN_SET_SLUG, version="v1", title="Golden Tasks")
            session.add(dataset)
            session.flush()

        created = updated = 0
        for path in files:
            task = TaskDefinition.model_validate_json(path.read_text(encoding="utf-8"))
            if upsert_task(session, task, environments):
                created += 1
            else:
                updated += 1

    print(f"Golden 题入库完成：新建 {created} 道，更新 {updated} 道")
    print("注意：没跑验证流水线（E1-T3），validation_state 停在 DISCOVERED")
    return 0


# ── enqueue ─────────────────────────────────────────────────


def cmd_enqueue(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        try:
            config = resolve_agent_config(session, agent=args.agent, config=args.config)
        except AgentConfigError as exc:
            print(exc)
            return 1

        # 题从**数据集快照**里取，不是从整张 benchmark_tasks 表里取。
        #
        # 早先这里写的是 `select id from benchmark_tasks`，不看数据集 —— 那时候库里
        # 只有四道 Golden 题，看不出问题。E8-T2 之后库里有 35 道，其中 9 道是人工
        # 终审否掉的 INVALID，照旧写法会把它们一起投进队列，而 Oracle 在坏题上
        # 必然掉出 100%，排查的人会去翻判定引擎（E1-T6 修）。
        try:
            dataset, note = resolve_set(session, args.set, args.version)
        except DatasetError as exc:
            print(exc)
            return 1
        if note:
            print(f"⚠ {note}")

        # 摘要算的是整份快照，选子集这件事由 manifest 的 selected_task_ids 记
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

        # 建实验这件事只有一份实现（`app.evaluation.orchestrator`）：
        # 这里和 `cli.experiment start` 走同一条路，不然 total_tasks 之类的字段
        # 迟早会有一边忘了写。可复现性清单也一样 —— `collect_provenance()` 是
        # 协议 C-27（脏工作区拒绝启动）的唯一强制点
        settings = get_settings()
        try:
            provenance = collect_provenance(
                session,
                benchmark_set_id=dataset.id,
                snapshot_digest=snapshot_digest(all_rows),
                agent_config_id=config.id,
                task_ids=task_ids,
                agent_concurrency=settings.agent_concurrency,
                sandbox_concurrency=settings.sandbox_concurrency,
                job_max_attempts=settings.job_max_attempts,
                allow_dirty=args.allow_dirty,
            )
            runs = create_runs(session, name=args.name, task_ids=task_ids, provenance=provenance)
        except (OrchestrationError, ProvenanceError) as exc:
            print(f"建不了：{exc}")
            return 1
        run_id = runs[0].id
        dirty = provenance.dirty

    print(f"实验 #{run_id}（{args.name}）已建，投了 {len(task_ids)} 条 EVAL_TASK 作业")
    if dirty:
        print("⚠ 工作区有未提交改动，这次实验已标 dirty=true（协议 C-28：不得进排行榜）")
    print("起 Worker 来跑：python -m app.worker")
    print(f"看进度 / 取消 / 补跑：python -m cli.experiment status --run {run_id}")
    return 0


# ── status ──────────────────────────────────────────────────


def cmd_status(_args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        rows = session.execute(
            sa.select(JobQueue.job_type, JobQueue.state, sa.func.count())
            .group_by(JobQueue.job_type, JobQueue.state)
            .order_by(JobQueue.job_type, JobQueue.state)
        ).all()
        leased = list(
            session.execute(
                sa.select(JobQueue.id, JobQueue.lease_owner, JobQueue.lease_expires_at)
                .where(JobQueue.state == JobState.LEASED)
                .order_by(JobQueue.id)
            ).all()
        )

    if not rows:
        print("队列是空的")
        return 0
    print(f"{'作业类型':<16} {'状态':<10} {'条数':>6}")
    for job_type, state, count in rows:
        print(f"{job_type.value:<16} {state.value:<10} {count:>6}")
    if leased:
        print("\n正在跑的：")
        for job_id, owner, expires in leased:
            print(f"  #{job_id}  {owner}  租约到 {expires:%H:%M:%S}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli.queue", description="作业队列")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed-golden", help="把 Golden 题写进库（开发用，不做验证）")
    p_seed.set_defaults(func=cmd_seed_golden)

    p_enqueue = sub.add_parser("enqueue", help="建一次实验并把题投进队列")
    p_enqueue.add_argument("--agent", default="oracle", help="Agent 名字，默认 oracle")
    p_enqueue.add_argument(
        "--config",
        help="配置标签（agent_configs.label）。该 Agent 有多份启用配置时必须给",
    )
    p_enqueue.add_argument("--set", default=GOLDEN_SET_SLUG, help="数据集 slug，默认 golden")
    p_enqueue.add_argument("--version", help="数据集版本，默认取最新已发布的那一版")
    p_enqueue.add_argument("--name", default="adhoc", help="实验名")
    p_enqueue.add_argument(
        "--task", action="append", help="只投这几道题（task_id，可重复给）。不给就投这一版全部"
    )
    p_enqueue.add_argument(
        "--allow-dirty",
        action="store_true",
        help="工作区不干净也建（协议 C-28：结果标 dirty=true，不得进排行榜）",
    )
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_status = sub.add_parser("status", help="看队列现状")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "upsert_task"]
