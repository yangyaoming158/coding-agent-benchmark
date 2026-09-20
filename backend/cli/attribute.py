"""失败归因的命令行（E6-T1 / E6-T2，`06-judge-attribution.md` §12.2）。

    python -m cli.attribute rules                     # 给全库的失败跑一遍规则分类
    python -m cli.attribute rules --redo              # 连已经判过的也重判
    python -m cli.attribute rules --dry-run           # 只看分布，不写库
    python -m cli.attribute rules --run-id 331        # 只判这一次
    python -m cli.attribute features --run-id 331     # 看一次运行的 Stage2 特征
    python -m cli.attribute llm --model deepseek/deepseek-chat --dry-run
    python -m cli.attribute llm --model deepseek/deepseek-chat

## 为什么是批量命令，不接进评测主流程

§12.4 最后一条：**归因挂了不能影响判定**。跑在主流程外面是最省事的保证 ——
主流程根本不认识归因这件事，它就不可能被归因拖垮。

另一层原因是 E6-T2：大模型归因必然要异步（缓存、低置信投票、失败退避），
两层归因应该共用一个入口。那个入口连同 `LifecycleStatus.ANALYZING`
一起留给 E6-T2，这一版不占坑。

## `--redo` 也不会盖掉人工结论

规则重跑只覆盖 `stage = RULE` 的行。人工抽检改过的（`HUMAN`）、大模型判过的
（`LLM`）一律不动，条件写在 SQL 的 WHERE 上，见 `app.attribution.persistence`。

## 这个模块为什么在 cli/ 而不是 app/

和 `cli/dataset.py` 一样：它要同时用 `app.attribution` 和数据库会话工厂，
还要建制品库。`app` 里没有一层该负责"把这三样凑起来跑一轮"。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import sqlalchemy as sa

from app.attribution.llm import AttributionRunError, attribute_failure, result_cache_hit
from app.attribution.persistence import (
    collect_features,
    existing_stages,
    load_facts,
    load_llm_input,
    save_llm_decisions,
    save_rule_verdicts,
)
from app.attribution.rules import RuleSkip, RuleVerdict, SkipReason, classify
from app.domain.enums import AttributionStage
from app.infrastructure.config import get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.llm import LLMClient, LLMConfigError
from app.infrastructure.models.attribution import FailureAttribution
from app.storage import create_artifact_store

#: 规则层至少要判死这么多失败，低于这条线说明分类器坏了（§12.2 给的下限）。
COVERAGE_FLOOR = 0.55


def cmd_rules(args: argparse.Namespace) -> int:
    """给失败跑一遍规则分类，打一张分布表。"""
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        rows = load_facts(session, task_run_ids=args.run_id or None)
        if not rows:
            print("一次运行都没有。先跑评测：`make enqueue && make worker`")
            return 1

        already = existing_stages(session)
        verdicts: list[tuple[int, RuleVerdict]] = []
        skipped: Counter[str] = Counter()
        by_category: Counter[str] = Counter()
        protected_by_hand = 0

        for row in rows:
            result = classify(row.facts)
            if isinstance(result, RuleSkip):
                skipped[result.reason.value] += 1
                continue
            stage = already.get(row.task_run_id)
            if stage in (AttributionStage.LLM, AttributionStage.HUMAN):
                protected_by_hand += 1
                continue
            if stage is AttributionStage.RULE and not args.redo:
                skipped["ALREADY_CLASSIFIED"] += 1
                continue
            verdicts.append((row.task_run_id, result))
            by_category[result.category.value] += 1

        _print_table(len(rows), by_category, skipped, protected_by_hand)

        if args.dry_run:
            print("\n--dry-run：没有写库")
            return 0

        report = save_rule_verdicts(session, verdicts)
        print(
            f"\n落库：新增 {report.inserted} 条、更新 {report.updated} 条、"
            f"保护 {report.protected} 条（已有 LLM/人工结论）"
        )
    return 0


def cmd_features(args: argparse.Namespace) -> int:
    """打印一次运行的 Stage2 特征（JSON）。给 E6-T2 和人工抽检看。"""
    factory = create_session_factory(create_db_engine())
    store = create_artifact_store()
    with session_scope(factory) as session:
        features = collect_features(session, store, args.run_id)
        print(json.dumps(features.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_llm(args: argparse.Namespace) -> int:
    """给规则层留下的 F1～F5 失败做批量归因。

    `--dry-run` 只组装 prompt 和显示指纹，**不创建客户端、不调模型**。
    这是为了让“先看会花多少钱”和“真的花钱”成为两个明确动作。
    """
    if args.limit is not None and args.limit < 1:
        print("--limit 必须大于等于 1", file=sys.stderr)
        return 2
    model = args.model or get_settings().judge_model
    if not model:
        print("没配模型：用 --model 指定，或在 .env 里设 JUDGE_MODEL", file=sys.stderr)
        return 1

    engine = create_db_engine()
    factory = create_session_factory(engine)
    store = create_artifact_store()
    prepared = []
    skipped: Counter[str] = Counter()
    try:
        with session_scope(factory) as session:
            rows = load_facts(session, task_run_ids=args.run_id or None)
            existing = {
                row.evaluation_task_run_id: row
                for row in session.scalars(sa.select(FailureAttribution))
            }
            for row in rows:
                routing = classify(row.facts)
                if not isinstance(routing, RuleSkip) or routing.reason is not SkipReason.NEEDS_LLM:
                    skipped["NOT_F1_TO_F5"] += 1
                    continue
                old = existing.get(row.task_run_id)
                if old is not None and old.stage in (AttributionStage.RULE, AttributionStage.HUMAN):
                    skipped[f"PROTECTED_{old.stage.value}"] += 1
                    continue
                payload = load_llm_input(session, store, row.task_run_id)
                if payload is None:
                    skipped["MISSING_INPUT"] += 1
                    continue
                prompt_hash = payload.prompt_hash()
                if (
                    old is not None
                    and old.stage is AttributionStage.LLM
                    and result_cache_hit(
                        old.judge_model,
                        old.prompt_hash,
                        model=model,
                        prompt_hash=prompt_hash,
                    )
                    and not args.redo
                ):
                    skipped["CACHED_DB"] += 1
                    continue
                prepared.append(payload)
    finally:
        engine.dispose()

    if args.limit is not None:
        prepared = prepared[: args.limit]

    print(f"待 LLM 归因 {len(prepared)} 次，模型 {model}")
    for name, count in sorted(skipped.items()):
        print(f"  跳过 {name:20} {count:>5}")
    if args.dry_run:
        for payload in prepared:
            print(f"  task_run={payload.task_run_id} prompt_sha256={payload.prompt_hash()}")
        print("\n--dry-run：没有调模型，没有写库")
        return 0
    if not prepared:
        return 0

    try:
        client = LLMClient.from_settings(model=model)
    except LLMConfigError as exc:
        print(f"没法调模型：{exc}", file=sys.stderr)
        return 1

    decisions = []
    failures = 0
    by_status: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    with client:
        for payload in prepared:
            try:
                decision = attribute_failure(payload, client, refresh=args.redo)
            except AttributionRunError as exc:
                failures += 1
                print(f"  ! task_run={payload.task_run_id} 归因失败：{exc}", file=sys.stderr)
                continue
            decisions.append((payload.task_run_id, decision))
            by_status[decision.status.value] += 1
            by_category[decision.verdict.category.value] += 1
            print(
                f"  {decision.status.value:11} {decision.verdict.category.value:38} "
                f"task_run={payload.task_run_id} confidence={decision.verdict.confidence:.3f}"
            )

    write_engine = create_db_engine()
    write_factory = create_session_factory(write_engine)
    try:
        with session_scope(write_factory) as session:
            report = save_llm_decisions(session, decisions)
    finally:
        write_engine.dispose()

    print("\n归因分布")
    for name, count in sorted(by_category.items()):
        print(f"  {name:40} {count:>5}")
    for name, count in sorted(by_status.items()):
        print(f"  status={name:32} {count:>5}")
    print(
        f"\n落库：新增 {report.inserted} 条、更新 {report.updated} 条、"
        f"保护 {report.protected} 条；失败 {failures} 条"
    )
    print(
        f"模型调用：{client.usage.calls} 次，文件缓存命中 "
        f"{client.usage.cached_calls} 次，计费调用 {client.usage.billed_calls} 次"
    )
    return 1 if failures else 0


def _print_table(
    total: int,
    by_category: Counter[str],
    skipped: Counter[str],
    protected_by_hand: int,
) -> None:
    """分布表。覆盖率按"规则判死 / 该归因的失败"算，不拿成功的运行当分母。"""
    decided = sum(by_category.values())
    already = skipped.get("ALREADY_CLASSIFIED", 0)
    needs_llm = skipped.get("NEEDS_LLM", 0)
    needs_control = skipped.get("NEEDS_CONTROL_RUN", 0)
    # 分母 = 所有该归因的失败：这一轮判死的 + 之前判过的 + 人工/大模型判过的
    #        + 还要大模型判的 + 等对照组的。成功和取消的不算。
    attributable = decided + already + protected_by_hand + needs_llm + needs_control

    print(f"一共 {total} 次运行，其中该归因的失败 {attributable} 次\n")
    print("  规则判出来的类")
    print("  " + "-" * 46)
    for name, count in sorted(by_category.items()):
        print(f"  {name:36} {count:>5}")
    if not by_category:
        print("  （这一轮一条都没判 —— 可能都判过了，加 --redo 重判）")

    print("\n  规则没判的")
    print("  " + "-" * 46)
    labels = {
        "RESOLVED": "成功，不归因",
        "NOT_FINISHED": "还没跑完",
        "CANCELLED": "人工取消，不算失败",
        "NEEDS_CONTROL_RUN": "要跑对照组才知道算谁的（E4-T5）",
        "NEEDS_LLM": "规则分不出 F1~F5，交给 E6-T2",
        "ALREADY_CLASSIFIED": "之前判过了（--redo 可重判）",
    }
    for name, count in sorted(skipped.items()):
        print(f"  {labels.get(name, name):36} {count:>5}")
    if protected_by_hand:
        print(f"  {'已有 LLM / 人工结论，不动':36} {protected_by_hand:>5}")

    if attributable:
        covered = decided + already + protected_by_hand
        ratio = covered / attributable
        verdict = "达标" if ratio >= COVERAGE_FLOOR else f"**低于 {COVERAGE_FLOOR:.0%}**"
        print(
            f"\n  规则层覆盖率 {covered}/{attributable} = {ratio:.1%}"
            f"（§12.2 下限 {COVERAGE_FLOOR:.0%}）—— {verdict}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.attribute", description="失败归因（E6）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rules = sub.add_parser("rules", help="规则前置分类（F6/F7/F8/N1）")
    p_rules.add_argument("--run-id", type=int, action="append", help="只判这次运行，可以给多个")
    p_rules.add_argument("--redo", action="store_true", help="连已经判过的也重判（人工结论仍不动）")
    p_rules.add_argument("--dry-run", action="store_true", help="只看分布，不写库")
    p_rules.set_defaults(func=cmd_rules)

    p_features = sub.add_parser("features", help="看一次运行的 Stage2 特征")
    p_features.add_argument("--run-id", type=int, required=True)
    p_features.set_defaults(func=cmd_features)

    p_llm = sub.add_parser("llm", help="LLM-as-Judge 归因（F1～F5）")
    p_llm.add_argument("--model", help="判定模型；缺省读 JUDGE_MODEL")
    p_llm.add_argument("--run-id", type=int, action="append", help="只判这次运行，可以给多个")
    p_llm.add_argument("--limit", type=int, help="最多处理多少次；首次付费试跑建议设 1")
    p_llm.add_argument(
        "--redo",
        action="store_true",
        help="重新调模型并覆盖旧 LLM 结论（可能产生费用）",
    )
    p_llm.add_argument(
        "--dry-run", action="store_true", help="只列出任务和 prompt 指纹，不调模型不写库"
    )
    p_llm.set_defaults(func=cmd_llm)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["COVERAGE_FLOOR", "build_parser", "cmd_features", "cmd_llm", "cmd_rules", "main"]
