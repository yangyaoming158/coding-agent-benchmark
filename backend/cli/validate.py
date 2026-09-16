"""题目验证流水线的命令行（E1-T3）。

    python -m cli.validate run                 # 验库里所有题
    python -m cli.validate run --task X --task Y
    python -m cli.validate run --dataset swebench-verified-subset --scope declared
    python -m cli.validate run --dry-run       # 只跑不写库
    python -m cli.validate show                # 看库里现在的验证状态

`make validate-tasks` 是 `run` 的快捷方式。

## 它和 `cli.queue seed-golden` 的分工

`seed-golden` 只把题目 JSON 原样写进 `benchmark_tasks`，`validation_state` 停在
`DISCOVERED`。真正判断"这道题立不立得住"的是这里：起容器、跑全量套件、
逐条比对 F2P / P2P，然后把结论和证据写回去。

只有 `VALID` 的题目能进数据集（§7.4）。所以真实题目**必须**先过这一关，
否则会有坏题混进去，Oracle 哨兵就不再是 100%（协议 C-50）。

## `--scope declared` 只给 P2P 已经给定的题用

默认 `--scope full`：S4 / S7 / S8 跑全量套件，P2P 候选池从全量报告里来（§7.2(6)）。
SWE-bench 官方题（E1-T7）的 P2P 是官方定的，不需要候选池，而它们的全量套件动辄
几十分钟 —— `--scope declared` 让三轮都只跑 F2P ∪ P2P，和正式评测跑的集合一样（C-17）。
挖掘出来的题**不要**用它：那样 P2P 候选池就是空的。证据文档里记着用的是哪一种。

## 拿不到结论时不动题目状态

平台自己出故障（连不上 docker、容器被 OOM 杀掉）时流水线返回 `state=None`。
这时命令把题目状态**恢复原样**，不写 INVALID —— 把平台故障记成题目无效，
下次就再没人会去查真正的原因了。这条和协议里"平台故障不算进解决率"是同一个道理。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.schema import TaskDefinition
from app.domain.enums import (
    ArtifactKind,
    ArtifactOwnerType,
    TaskValidationState,
)
from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE
from app.evaluation.validation import (
    DEFAULT_REPEAT,
    ValidationRequest,
    ValidationResult,
    summarize,
    validate_task,
)
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.benchmark import BenchmarkTask, EnvironmentSpec
from app.storage import create_artifact_store
from app.storage.base import ArtifactStore


@dataclass(frozen=True)
class _Loaded:
    """从库里读出来的一道题，加上跑验证要的环境信息。"""

    row_id: int
    task: TaskDefinition
    image: str
    extra_protected_paths: tuple[str, ...]
    environment_spec_id: int
    previous_state: TaskValidationState


def _load(
    session: Session, task_ids: Sequence[str], dataset_id: str | None = None
) -> list[_Loaded]:
    """读题。`raw_definition` 里存的是完整的题目 JSON，从它还原 `TaskDefinition`。

    不从那十几个列拼：列只是给 SQL 查询用的投影，`test_patch` 和 `gold_patch`
    根本不在列里，而这两样是验证的核心输入。

    `dataset_id` 按 `raw_definition->>'dataset_id'` 筛：题目的归属只写在那里
    （§8.11 第十节说明了为什么 `benchmark_tasks` 不加 `dataset_id` 列）。
    """
    query = (
        sa.select(BenchmarkTask, EnvironmentSpec)
        .join(EnvironmentSpec, BenchmarkTask.environment_spec_id == EnvironmentSpec.id)
        .order_by(BenchmarkTask.task_id)
    )
    if task_ids:
        query = query.where(BenchmarkTask.task_id.in_(task_ids))
    if dataset_id:
        query = query.where(BenchmarkTask.raw_definition["dataset_id"].astext == dataset_id)

    loaded = []
    for row, env in session.execute(query).all():
        if not row.raw_definition:
            raise SystemExit(f"题目 {row.task_id} 的 raw_definition 是空的，没法验；先重新入库")
        loaded.append(
            _Loaded(
                row_id=row.id,
                task=TaskDefinition.model_validate(row.raw_definition),
                # E2-T3 之前 image_tag 还是空的，退回 Golden 那个手写镜像
                image=env.image_tag or DEFAULT_GOLDEN_IMAGE,
                extra_protected_paths=tuple(env.extra_protected_paths or ()),
                environment_spec_id=env.id,
                previous_state=row.validation_state,
            )
        )
    return loaded


def _build_request(
    loaded: _Loaded,
    settings: Settings,
    scratch_dir: Path,
    *,
    repeat: int,
    suite_scope: Literal["full", "declared"] = "full",
) -> ValidationRequest:
    task = loaded.task
    return ValidationRequest(
        plan=task.execution_plan(extra_protected_paths=loaded.extra_protected_paths),
        gold_patch=task.gold_patch,
        repo_name=task.repo_name,
        mirror_root=Path(settings.mirror_root),
        scratch_dir=scratch_dir,
        image=loaded.image,
        repo_url=task.repo_url,
        content_hash=task.content_hash,
        review_flags=tuple(task.review_flags()),
        previous_state=loaded.previous_state,
        repeat=repeat,
        suite_scope=suite_scope,
    )


def _mark_validating(session: Session, row_id: int) -> None:
    """开跑之前先把状态推到 `VALIDATING`（§7.4）。

    进程中途被杀的话，题目会停在 `VALIDATING` —— 这是有意的：它看得出来
    "验到一半没结果"，比停在 `DISCOVERED` 装作什么都没发生好。
    """
    session.execute(
        sa.update(BenchmarkTask)
        .where(BenchmarkTask.id == row_id)
        .values(validation_state=TaskValidationState.VALIDATING)
    )


def _write_back(
    session: Session,
    loaded: _Loaded,
    result: ValidationResult,
    store_backend_rows: bool = True,
) -> None:
    """把结论写回 `benchmark_tasks`，顺带记下证据制品和镜像 digest。"""
    if result.state is None:
        # 没得出结论 → 状态恢复原样。写 INVALID 是在冤枉题目
        session.execute(
            sa.update(BenchmarkTask)
            .where(BenchmarkTask.id == loaded.row_id)
            .values(validation_state=loaded.previous_state)
        )
        return

    session.execute(
        sa.update(BenchmarkTask)
        .where(BenchmarkTask.id == loaded.row_id)
        .values(
            validation_state=result.state,
            invalid_reason_code=result.reason_code.value if result.reason_code else None,
            validated_at=result.validated_at,
            validation_evidence_uri=result.evidence_uri,
        )
    )

    # 协议 C-36：引用镜像要用 digest 不用 tag。验证时拿到了就顺手记下来，
    # 免得以后有人得回头一个个 inspect
    if result.image is not None and result.image.digest:
        session.execute(
            sa.update(EnvironmentSpec)
            .where(EnvironmentSpec.id == loaded.environment_spec_id)
            .values(image_digest=result.image.digest)
        )

    if not store_backend_rows:
        return
    for ref in result.artifacts.values():
        session.add(
            Artifact(
                owner_type=ArtifactOwnerType.VALIDATION,
                owner_id=loaded.row_id,
                kind=ArtifactKind.VALIDATION_EVIDENCE,
                uri=ref.uri,
                backend=ref.backend,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
                compressed=ref.compressed,
            )
        )


def _print_steps(task_id: str, result: ValidationResult) -> None:
    mark = "✓" if result.valid else "✗"
    label = result.state.value if result.state else "没得出结论"
    print(f"\n{mark} {task_id}  →  {label}")
    for step in result.steps:
        icon = "✓" if step.ok else "✗"
        print(f"    {icon} {step.step} {step.name} —— {step.detail}（{step.duration_ms} ms）")
    if result.reason_code:
        print(f"    reason_code = {result.reason_code.value}")
    if result.error:
        print(f"    平台故障：{result.error}")
    review = result.evidence.get("review")
    if review:
        print(f"    需人工复核：{review}")
    if result.evidence_uri:
        print(f"    证据：{result.evidence_uri}")


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    store: ArtifactStore | None = None if args.dry_run else create_artifact_store(settings)
    engine = create_db_engine()
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        loaded_all = _load(session, args.task or [], args.dataset)
    if not loaded_all:
        print("库里没有匹配的题目，先跑 `make seed-tasks`", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: list[tuple[str, ValidationResult]] = []

    for loaded in loaded_all:
        task_id = loaded.task.task_id
        if not args.dry_run:
            with session_scope(factory) as session:
                _mark_validating(session, loaded.row_id)

        scratch = Path(settings.workspace_root) / f"validate-{stamp}" / task_id
        try:
            result = validate_task(
                _build_request(
                    loaded, settings, scratch, repeat=args.repeat, suite_scope=args.scope
                ),
                store=store,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        results.append((task_id, result))
        _print_steps(task_id, result)

        if not args.dry_run:
            with session_scope(factory) as session:
                _write_back(session, loaded, result)

    print("\n" + summarize(results))
    if args.dry_run:
        print("（--dry-run：没有写库，也没有落制品）")

    # 有一道题没验成 VALID 就非零退出。CI 和 `make` 靠退出码判断，
    # 光靠打印的字没人会去看
    return 0 if all(r.valid for _, r in results) else 1


def cmd_show(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        query = sa.select(
            BenchmarkTask.task_id,
            BenchmarkTask.validation_state,
            BenchmarkTask.invalid_reason_code,
            BenchmarkTask.validated_at,
            BenchmarkTask.validation_evidence_uri,
        ).order_by(BenchmarkTask.task_id)
        if args.task:
            query = query.where(BenchmarkTask.task_id.in_(args.task))
        rows = list(session.execute(query).all())

    if not rows:
        print("库里没有题目，先跑 `make seed-tasks`")
        return 0
    print(f"{'题目':34} {'状态':16} {'reason_code':18} 验于")
    for task_id, state, reason, validated_at, _uri in rows:
        when = validated_at.strftime("%Y-%m-%d %H:%M") if validated_at else "—"
        print(f"{task_id:34} {state.value:16} {reason or '—':18} {when}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.validate", description="题目验证流水线（E1-T3，§7.3 八步）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑八步验证并把结论写回数据库")
    p_run.add_argument("--task", action="append", help="只验这几道题（task_id，可重复给）")
    p_run.add_argument(
        "--dataset", help="只验这个 dataset_id 的题（raw_definition->>'dataset_id'）"
    )
    p_run.add_argument(
        "--scope",
        choices=("full", "declared"),
        default="full",
        help="full = 跑全量套件（默认，挖掘题必须）；declared = 只跑 F2P ∪ P2P（官方导入题）",
    )
    p_run.add_argument("--dry-run", action="store_true", help="只跑不写库、不落制品")
    p_run.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help=f"gold 侧跑几遍找不稳定用例，默认 {DEFAULT_REPEAT}；给 1 就跳过这项检查",
    )
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="看库里现在的验证状态")
    p_show.add_argument("--task", action="append", help="只看这几道题")
    p_show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
