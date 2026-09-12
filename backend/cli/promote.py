"""把候选推成题目（E8-T2，`03-benchmark-spec.md` §8.4 第九、十步）。

    python -m cli.promote probe      # 探测轮：实测证伪 F2P，派生 P2P
    python -m cli.promote show       # 看探测结果

## 为什么要单独有一轮"探测"

一道题必须有 `pass_to_pass`，而 §7.2(6) 把它定义成"在 `base + test_patch` 上就通过、
打上 `gold_patch` 之后仍然通过的用例" —— **两句话都只能靠真跑一遍测试回答**。
E1-T5 停在 `PRESCREENED` 就是因为它一个容器都不起。

所以顺序是：

    探测（2 个容器）→ 组装定稿题目 → 八步验证（3 个容器，题目一个字都不改）

探测**就是** `validate_task`，只是把 `pass_to_pass` 传空、`repeat=1`：
S4 给基线全量报告，S5 拿它逐条证伪 F2P，S6/S7 确认 gold 真的修好了，
P2P 候选池就是 S4 和 S7 两份报告的通过集交集。不另写一套跑测试的代码，
理由见 §7.10「跑测试复用 `execute_tests`，没有第二套实现」。

## 探测不落制品、不改题目状态

探测轮的结论不是"这道题验过了"—— 它用的是一份 P2P 为空的临时题目，
`content_hash` 和最终题目对不上。正式的证据由 `cli.validate run` 在题目定稿之后产出。
所以这里只把结果记进 `task_candidates.raw_payload.probe`，P2P 名单落
`var/promote/<repo>/<pr>.probe.json`（几千条 ID，塞进 JSONB 会把库撑肥，
而且组装完它还会在 `raw_definition` 里存第二份）。
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.assembly import (
    AssemblyError,
    Candidate,
    Environment,
    F2PSelection,
    assemble,
    environment_from_recipe,
    load_candidate,
    select_f2p,
    select_p2p,
)
from app.benchmark.schema import P2PSampling
from app.domain.enums import InfraOutcome, TaskCandidateState, TaskValidationState
from app.domain.execution_plan import ExecutionPlan
from app.evaluation.executor import ExecutionOutcome, execute_tests
from app.evaluation.validation import PLATFORM_FAULTS
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import BenchmarkTask, Repository, TaskCandidate
from app.sandbox.images import load_recipe
from app.sandbox.mirror import MirrorManager
from cli.queue import upsert_task

#: 仓库根目录（这个文件在 `backend/cli/` 底下）。
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECIPE_DIR = REPO_ROOT / "images" / "envs"
PATCH_ROOT = REPO_ROOT / "var" / "mining" / "patches"
PROBE_ROOT = REPO_ROOT / "var" / "promote"

#: 改了探测逻辑就升它。存进 `raw_payload.probe`，用来认出哪些结果该重跑。
PROBER_VERSION = "1.2"

#: 默认造 `benchmark-dev`（§8.1 的 L1）。
DEFAULT_DATASET_ID = "benchmark-dev"

#: 只有这两档往下走：`REJECT` 按 §8.10 第二节实测误杀率 0%，可以放心丢。
PROMOTABLE_DECISIONS = ("PASS", "REVIEW")


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """一条候选探测完之后我们知道的东西。"""

    task_id: str
    ok: bool
    #: 分档，进漏斗报表：OK / ASSEMBLY_FAILED / F2P_NOT_FAILING / ENV_NOT_RUNNABLE /
    #: TEST_TOO_SLOW / REVIEW_REQUIRED / NO_VERDICT
    bucket: str
    detail: str
    reason_code: str | None = None
    f2p_count: int = 0
    p2p_count: int = 0
    suite_seconds: float = 0.0


# ══════════════════════════════════════════════════════════════
# 读候选
# ══════════════════════════════════════════════════════════════


def _select_candidates(
    session: Session,
    *,
    repo: str | None,
    prs: Sequence[int],
    decisions: Sequence[str],
    branch: str | None,
    limit: int | None,
    redo: bool,
    include_promoted: bool = False,
) -> list[tuple[int, dict[str, Any]]]:
    """挑出这一轮要探测（或组装）的候选，返回 `(行 id, raw_payload)`。

    默认跳过已经探测过、而且探测器版本没变的 —— 探测一条要起两个容器，
    重跑一遍全库很贵。`--redo` 强制重来。

    `include_promoted` 是给**组装**用的另一个开关：已经推成题目的候选是 `PROMOTED`，
    正常不该再做一遍，但派生规则改了就必须能重做。
    """
    # `redo` 顺带放开状态过滤：已经推成题目的候选是 `PROMOTED`，正常情况下不该再做一遍，
    # 但**派生规则改了就必须能重做**。2026-09-10 E1-T6 撞到这一条：`select_p2p()` 加了
    # 一道剔除不稳定用例的过滤，22 道题的 `pass_to_pass` 要按新规则重算，
    # 而候选早就是 `PROMOTED` 了 —— 表现是"一条候选都选不出来"，看起来像缓存坏了。
    states = (
        (TaskCandidateState.PRESCREENED, TaskCandidateState.PROMOTED)
        if include_promoted
        else (TaskCandidateState.PRESCREENED,)
    )
    query = (
        sa.select(TaskCandidate.id, TaskCandidate.raw_payload)
        .join(Repository, Repository.id == TaskCandidate.repository_id)
        .where(TaskCandidate.state.in_(states))
        .order_by(TaskCandidate.pr_number)
    )
    if repo:
        query = query.where(Repository.full_name == repo)
    if prs:
        query = query.where(TaskCandidate.pr_number.in_(prs))

    rows: list[tuple[int, dict[str, Any]]] = []
    for row_id, payload in session.execute(query).all():
        data = dict(payload)
        prescreen = data.get("prescreen") or {}
        if decisions and str(prescreen.get("decision")) not in decisions:
            continue
        pr = data.get("pr") or {}
        if branch and str(pr.get("base_ref_name")) != branch:
            continue
        cleaned = data.get("cleaned") or {}
        if not cleaned.get("f2p_candidates"):
            # 抽不出候选 F2P 的候选组装不出题目（§8.10 第四节实测 80 条里有 10 条）。
            # 起两个容器去证明这件事纯属浪费，这里直接跳过
            continue
        probe = data.get("probe") or {}
        if not redo and probe.get("prober_version") == PROBER_VERSION:
            continue
        rows.append((row_id, data))
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _environment_for(repo_name: str, python_version: str = "py311") -> Environment:
    """按仓库名找环境配方。

    命名规则和 `images/envs/*.json` 的文件名一致：`owner__repo__py311`。
    找不到就当场报错 —— 猜一个环境跑出来的结论是不可信的。
    """
    owner, _, repo = repo_name.partition("/")
    environment_id = f"{owner}__{repo}__{python_version}"
    path = ENV_RECIPE_DIR / f"{environment_id}.json"
    if not path.exists():
        raise AssemblyError(f"没有 {repo_name} 的环境配方：{path}")
    return environment_from_recipe(load_recipe(path))


# ══════════════════════════════════════════════════════════════
# 探测一条
# ══════════════════════════════════════════════════════════════


class _SuiteFailure(Exception):  # noqa: N818 —— 这是控制流，不是错误，和 validation._Stop 同理
    """跑一轮全量套件没跑成。`bucket` 决定它落在漏斗的哪一格。"""

    def __init__(self, bucket: str, detail: str) -> None:
        super().__init__(detail)
        self.bucket = bucket
        self.detail = detail


def _run_full_suite(
    plan: ExecutionPlan,
    patch: str,
    *,
    image: str,
    mirror_root: Path,
    repo_name: str,
    workspace: Path,
) -> dict[str, str]:
    """在容器里跑一遍**全量**套件，返回"用例 ID → 状态"。

    走 `execute_tests` 是刻意的：正式评测、八步验证走的都是它，四者共用同一套容器规格、
    断网策略、补丁应用顺序和报告解析（§7.10）。探测**不是**验证 —— 它用的是一份
    还没有 P2P 的临时题目，`content_hash` 和定稿题目对不上，所以这里不走
    `validate_task` 的八步，也不落验证制品。定稿题目的正式证据由 `cli.validate run` 产出。
    """
    outcome: ExecutionOutcome = execute_tests(
        plan,
        patch,
        mirror_path=MirrorManager(mirror_root).path_for(repo_name),
        workspace_dir=workspace,
        image=image,
        test_ids=(),  # 空 = 跑全量。子集拿不到 P2P 候选池，也看不见套件里别的用例
    )
    if outcome.infra_outcome is InfraOutcome.TEST_TIMEOUT:
        raise _SuiteFailure("TEST_TOO_SLOW", f"全量套件超过 {plan.test_timeout_s} 秒还没跑完")
    if outcome.infra_outcome in PLATFORM_FAULTS:
        # 平台自己的故障不能算到题目头上（§7.10「三种失败，结论不一样」第三行）
        raise _SuiteFailure(
            "NO_VERDICT", f"平台故障（{outcome.infra_outcome.value}）：{outcome.problem}"
        )
    if outcome.report is None:
        raise _SuiteFailure("REVIEW_REQUIRED", f"{outcome.infra_outcome.value}：{outcome.problem}")
    if not outcome.report.cases:
        # 一条用例都没收集到 = 环境跑不起来，不是题目坏了。分开记，因为处置相反：
        # 前者要修环境，后者要丢题（2026-09-10 实测踩过，见 validation.py 里同一处判断）
        tail = (outcome.container.stdout or "")[-400:] if outcome.container else ""
        raise _SuiteFailure("ENV_NOT_RUNNABLE", f"套件一条用例都没收集到；输出尾部：{tail}")
    return {case_id: case.status.value for case_id, case in outcome.report.cases.items()}


def _derive(
    candidate: Candidate,
    baseline: dict[str, str],
    gold: dict[str, str],
    suite_seconds: float,
) -> tuple[F2PSelection, dict[str, Any]]:
    """两份全量报告 → 定稿题目要的 `fail_to_pass` 和 `pass_to_pass`。

    F2P 按 §7.2(5) 实测证伪（基线上失败、打完 gold 通过），
    P2P 按 §7.2(6) 取两轮通过集的**交集**。只用基线那一半的话，凡是 gold 顺带改了
    行为的用例都会在正式验证的 S8 被记成 `GOLD_REGRESSION`，好题被丢掉，理由还是错的。
    """
    f2p = select_f2p(
        candidates=candidate.f2p_candidates, baseline_status=baseline, gold_status=gold
    )
    p2p = select_p2p(
        baseline_passing=[c for c, st in baseline.items() if st == "PASSED"],
        gold_passing=[c for c, st in gold.items() if st == "PASSED"],
        fail_to_pass=f2p.ids,
        suite_seconds=suite_seconds,
        gold_patch=candidate.gold_patch,
    )
    derived = {
        "fail_to_pass": list(f2p.ids),
        "pass_to_pass": list(p2p.ids),
        "p2p_sampling": p2p.sampling.model_dump(mode="json"),
        "suite_seconds": round(suite_seconds, 3),
        "baseline_total": len(baseline),
        "f2p_candidates": list(candidate.f2p_candidates),
        "f2p_unmatched": list(f2p.unmatched),
        "f2p_uncollectable_on_base": list(f2p.uncollectable_on_base),
        "f2p_not_failing": f2p.not_failing or {},
        "f2p_not_fixed": list(f2p.not_fixed),
        "dropped_unusable": list(f2p.dropped_unusable) + list(p2p.dropped_unusable),
        "lost_after_gold": list(p2p.lost_after_gold),
    }
    return f2p, derived


def _probe_file(candidate: Candidate) -> Path:
    owner, _, repo = candidate.repo_name.partition("/")
    return PROBE_ROOT / f"{owner}__{repo}" / f"{candidate.pr_number}.probe.json"


def _summary_for_payload(derived: dict[str, Any], probe_file: Path) -> dict[str, Any]:
    """进 `raw_payload.probe` 的那份摘要 —— 只放数，不放几千条 ID。"""
    return {
        "f2p_count": len(derived["fail_to_pass"]),
        "p2p_count": len(derived["pass_to_pass"]),
        "p2p_strategy": derived["p2p_sampling"]["strategy"],
        "suite_seconds": derived["suite_seconds"],
        "baseline_total": derived["baseline_total"],
        "f2p_unmatched": len(derived["f2p_unmatched"]),
        "f2p_uncollectable_on_base": len(derived["f2p_uncollectable_on_base"]),
        "f2p_not_failing": len(derived["f2p_not_failing"]),
        "f2p_not_fixed": len(derived["f2p_not_fixed"]),
        "dropped_unusable": len(derived["dropped_unusable"]),
        "lost_after_gold": len(derived["lost_after_gold"]),
        "p2p_file": str(probe_file.relative_to(REPO_ROOT)),
    }


def _reuse_probe_file(candidate: Candidate) -> tuple[ProbeOutcome, dict[str, Any]] | None:
    """磁盘上已经有这一条的探测结果就直接用，不再起容器。

    **这是为清库准备的。** 探测结果同时存在两个地方：`raw_payload.probe`（库里）
    和 `var/promote/<repo>/<pr>.probe.json`（磁盘）。清库这件事免不了（AGENTS.md §9），
    而重探一遍 51 条候选是十几分钟的容器时间，
    换回来的是**一模一样的结果** —— 同一个 base_commit、同一个镜像、同一份补丁。

    版本对不上就不复用：探测逻辑改过之后旧结果不能要（`PROBER_VERSION`）。
    真想重跑就加 `--redo`。
    """
    path = _probe_file(candidate)
    if not path.exists():
        return None
    derived = json.loads(path.read_text(encoding="utf-8"))
    if derived.get("prober_version") != PROBER_VERSION:
        return None

    record: dict[str, Any] = {
        "probed_at": datetime.now(UTC).isoformat(),
        "prober_version": PROBER_VERSION,
        "state": "OK",
        "detail": "复用磁盘上的探测结果，没有起容器",
        **_summary_for_payload(derived, path),
    }
    return (
        ProbeOutcome(
            candidate.task_id,
            True,
            "OK",
            f"{len(derived['fail_to_pass'])} 条 F2P，{len(derived['pass_to_pass'])} 条 P2P（复用）",
            f2p_count=len(derived["fail_to_pass"]),
            p2p_count=len(derived["pass_to_pass"]),
            suite_seconds=float(derived["suite_seconds"]),
        ),
        record,
    )


def _probe_one(
    candidate: Candidate,
    environment: Environment,
    settings: Settings,
    scratch: Path,
    *,
    reuse: bool = True,
) -> tuple[ProbeOutcome, dict[str, Any] | None]:
    """探一条：起两个容器，一个空补丁一个 gold 补丁。"""
    if reuse:
        cached = _reuse_probe_file(candidate)
        if cached is not None:
            return cached

    now = datetime.now(UTC).isoformat()
    try:
        task = assemble(candidate, environment, dataset_id=DEFAULT_DATASET_ID)
    except Exception as exc:  # pydantic 的 ValidationError 也在里面
        # 组装就失败 = 连合法题目都拼不出来（issue 泄题、gold 碰受保护路径……）。
        # 它是漏斗里独立的一格，和"跑出来不合格"不是一回事
        detail = str(exc).strip()
        return (
            ProbeOutcome(candidate.task_id, False, "ASSEMBLY_FAILED", detail),
            {
                "probed_at": now,
                "prober_version": PROBER_VERSION,
                "state": "ASSEMBLY_FAILED",
                "detail": detail,
            },
        )

    plan = task.execution_plan(extra_protected_paths=environment.extra_protected_paths)
    image = environment.image_tag
    mirror_root = Path(settings.mirror_root)
    repo_name = task.repo_name
    started = time.monotonic()
    try:
        baseline = _run_full_suite(
            plan,
            "",
            image=image,
            mirror_root=mirror_root,
            repo_name=repo_name,
            workspace=scratch / "baseline",
        )
        suite_seconds = time.monotonic() - started
        gold = _run_full_suite(
            plan,
            task.gold_patch,
            image=image,
            mirror_root=mirror_root,
            repo_name=repo_name,
            workspace=scratch / "gold",
        )
    except _SuiteFailure as failure:
        return (
            ProbeOutcome(candidate.task_id, False, failure.bucket, failure.detail),
            {
                "probed_at": now,
                "prober_version": PROBER_VERSION,
                "state": failure.bucket,
                "detail": failure.detail,
            },
        )

    f2p, derived = _derive(candidate, baseline, gold, suite_seconds)
    record: dict[str, Any] = {"probed_at": now, "prober_version": PROBER_VERSION}

    if not f2p.ids:
        # 一条都没证伪成功 —— 这道题不揭示 bug（§7.2(5)），丢掉
        detail = (
            f"候选 {len(candidate.f2p_candidates)} 条：报告里没有 {len(f2p.unmatched)} 条、"
            f"base 上收集不出来 {len(f2p.uncollectable_on_base)} 条、"
            f"基线上就通过 {len(f2p.not_failing or {})} 条、gold 没修好 {len(f2p.not_fixed)} 条"
        )
        record.update(state="F2P_NOT_FAILING", detail=detail)
        return ProbeOutcome(candidate.task_id, False, "F2P_NOT_FAILING", detail), record

    probe_file = _probe_file(candidate)
    probe_file.parent.mkdir(parents=True, exist_ok=True)
    probe_file.write_text(
        json.dumps(
            {"task_id": candidate.task_id, "prober_version": PROBER_VERSION, **derived},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    record.update(state="OK", detail="", **_summary_for_payload(derived, probe_file))

    return (
        ProbeOutcome(
            candidate.task_id,
            True,
            "OK",
            f"{len(f2p.ids)} 条 F2P，{len(derived['pass_to_pass'])} 条 P2P"
            f"（{derived['p2p_sampling']['strategy']}）",
            f2p_count=len(f2p.ids),
            p2p_count=len(derived["pass_to_pass"]),
            suite_seconds=derived["suite_seconds"],
        ),
        record,
    )


# ══════════════════════════════════════════════════════════════
# probe
# ══════════════════════════════════════════════════════════════


def cmd_probe(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_db_engine()
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        picked = _select_candidates(
            session,
            repo=args.repo,
            prs=args.pr or [],
            decisions=args.decision or list(PROMOTABLE_DECISIONS),
            branch=args.branch,
            limit=args.limit,
            redo=args.redo,
        )
    if not picked:
        print("没有要探测的候选（都探过了？加 --redo 重来）", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    outcomes: list[ProbeOutcome] = []
    print(f"要探 {len(picked)} 条\n")

    for row_id, payload in picked:
        try:
            candidate = load_candidate(payload, patch_root=PATCH_ROOT)
            environment = _environment_for(candidate.repo_name)
        except AssemblyError as exc:
            pr = (payload.get("pr") or {}).get("number")
            outcomes.append(ProbeOutcome(f"#{pr}", False, "ASSEMBLY_FAILED", str(exc)))
            print(f"  ✗ #{pr:<6} ASSEMBLY_FAILED  {exc}")
            continue

        scratch = Path(settings.workspace_root) / f"probe-{stamp}" / candidate.task_id
        try:
            outcome, record = _probe_one(
                candidate, environment, settings, scratch, reuse=not args.redo
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        outcomes.append(outcome)
        mark = "✓" if outcome.ok else "✗"
        code = outcome.reason_code or outcome.bucket
        print(f"  {mark} #{candidate.pr_number:<6} {code:<18} {outcome.detail[:96]}")

        if record is not None and not args.dry_run:
            with session_scope(factory) as session:
                _write_probe(session, row_id, record)

    print("\n" + _summarize(outcomes))
    if args.dry_run:
        print("（--dry-run：没有写库）")
    return 0


def _write_probe(session: Session, row_id: int, record: dict[str, Any]) -> None:
    """把探测结果并进 `raw_payload.probe`。

    读出来整体replace，不用 JSONB 的原地更新：`raw_payload` 是 `dict` 类型的列，
    SQLAlchemy 只在整个值被换掉时才认为它脏了，改子键不会落库（改了不报错，只是没写进去）。
    """
    row = session.get(TaskCandidate, row_id)
    if row is None:
        return
    payload = dict(row.raw_payload)
    payload["probe"] = record
    row.raw_payload = payload


def _summarize(outcomes: Sequence[ProbeOutcome]) -> str:
    from collections import Counter

    buckets = Counter(o.bucket if o.ok else (o.reason_code or o.bucket) for o in outcomes)
    ok = [o for o in outcomes if o.ok]
    lines = [f"探了 {len(outcomes)} 条，能往下走 {len(ok)} 条（{len(ok) / len(outcomes):.0%}）", ""]
    for name, count in buckets.most_common():
        lines.append(f"  {name:<20} {count}")
    if ok:
        f2p = sorted(o.f2p_count for o in ok)
        lines.append("")
        lines.append(f"  F2P 条数     中位 {f2p[len(f2p) // 2]}，最少 {f2p[0]}，最多 {f2p[-1]}")
        p2p = sorted(o.p2p_count for o in ok)
        secs = sorted(o.suite_seconds for o in ok)
        lines.append(f"  P2P 条数     中位 {p2p[len(p2p) // 2]}，最少 {p2p[0]}，最多 {p2p[-1]}")
        lines.append(f"  套件耗时     中位 {secs[len(secs) // 2]:.1f} 秒，最慢 {secs[-1]:.1f} 秒")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# assemble
# ══════════════════════════════════════════════════════════════


def _environment_spec_row(environment: Environment) -> dict[str, Any]:
    """`upsert_environment()` 要的那份规格字典。

    装依赖和跑测试的命令由题目自己带（它们已经从配方翻过来了），这里只补
    `upsert_environment` 从题目上取不到的那几样。`image_tag` 尤其重要：
    `cli.images build` 的 `write_back()` **只更新已有的行、不建行**，
    所以必须先有这一行，digest 才写得回来。
    """
    return {
        "python_version": environment.python_version,
        "extra_protected_paths": list(environment.extra_protected_paths),
        "image_tag": environment.image_tag,
    }


def _dataset_size(session: Session, dataset_id: str) -> int:
    """数据集里现在有多少道**还在数据集里**的题。

    人工终审否掉的（`INVALID`）和复验隔离的（`QUARANTINED`）不算 ——
    它们已经退出这个数据集了，占着名额只会让人补不进新题。
    """
    rows = session.execute(
        sa.select(BenchmarkTask.validation_state, BenchmarkTask.raw_definition)
    ).all()
    out = 0
    for state, definition in rows:
        if (definition or {}).get("dataset_id") != dataset_id:
            continue
        if state in (TaskValidationState.INVALID, TaskValidationState.QUARANTINED):
            continue
        out += 1
    return out


def _spread(rows: list[tuple[int, dict[str, Any]]], count: int) -> list[tuple[int, dict[str, Any]]]:
    """按 PR 号等距抽 `count` 条，而不是取最前面的几条。

    候选是按 PR 号排序的，也就是按时间排序。取前 N 条会让整批题挤在两年前，
    取后 N 条会挤在最近几个月 —— 两种都让"这个题库覆盖多长的时间窗"变成假的，
    而 base commit 离环境镜像快照有多远，正是实测出来的主要风险来源（§8.9 第六节）。

    等距抽样是确定性的：同一批候选每次抽出同一批题，不需要记种子。
    """
    step = len(rows) / count
    return [rows[int(i * step)] for i in range(count)]


def cmd_assemble(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        picked = _select_candidates(
            session,
            repo=args.repo,
            prs=args.pr or [],
            decisions=list(PROMOTABLE_DECISIONS),
            branch=None,
            limit=None,
            # 这里的筛选靠探测结果，不靠"探过没有"，所以恒为 True；
            # 要不要连 `PROMOTED` 的一起重做由 `--redo` 决定（见下）
            redo=True,
            include_promoted=args.redo,
        )

    ready = [
        (row_id, payload)
        for row_id, payload in picked
        if (payload.get("probe") or {}).get("state") == "OK"
        and (payload.get("probe") or {}).get("prober_version") == PROBER_VERSION
    ]
    if not ready:
        print(f"没有探测通过的候选（探测器版本要是 {PROBER_VERSION}）", file=sys.stderr)
        return 1

    if args.limit is not None:
        # `--limit` 是**数据集最终要有多少道**，不是"这一次加多少道"。
        #
        # 差别很要命：按"这一次加多少"解释的话，同一条命令跑两遍会从剩下的候选里
        # 接着做，把数据集从 30 道撑到 51 道 —— **不产生重复题，但集合悄悄变大了**，
        # 而这比重复更难发现（2026-09-10 实测踩到）。按"最终要有多少"解释，
        # 跑第二遍就是空操作，想加题把数字调大即可。
        with session_scope(factory) as session:
            have = _dataset_size(session, args.dataset_id)
        room = args.limit - have
        if room <= 0:
            print(f"{args.dataset_id} 已经有 {have} 道题，不用再加（要加就把 --limit 调大）")
            return 0
        if room < len(ready):
            ready = _spread(ready, room)
        print(f"{args.dataset_id} 现有 {have} 道，这一轮补到 {have + len(ready)} 道")

    print(f"要入库 {len(ready)} 道题\n")
    created = updated = failed = 0

    for row_id, payload in ready:
        candidate = load_candidate(payload, patch_root=PATCH_ROOT)
        environment = _environment_for(candidate.repo_name)
        probe = json.loads(_probe_file(candidate).read_text(encoding="utf-8"))

        try:
            task = assemble(
                candidate,
                environment,
                dataset_id=args.dataset_id,
                fail_to_pass=probe["fail_to_pass"],
                pass_to_pass=probe["pass_to_pass"],
                p2p_sampling=P2PSampling.model_validate(probe["p2p_sampling"]),
            )
        except Exception as exc:  # 组装失败要逐条报，不能整批崩
            failed += 1
            print(f"  ✗ #{candidate.pr_number:<6} 组装失败：{str(exc).strip()[:120]}")
            continue

        if args.dry_run:
            created += 1
            print(f"  · #{candidate.pr_number:<6} {task.task_id:<26} {task.difficulty.value:<7}")
            continue

        with session_scope(factory) as session:
            is_new = upsert_task(
                session,
                task,
                {task.environment_id: _environment_spec_row(environment)},
                patch_uri_scheme="mined",
            )
            # 候选到此为止：它已经变成题目了（§7.4 的 CANDIDATE → VALIDATING）
            row = session.get(TaskCandidate, row_id)
            if row is not None:
                row.state = TaskCandidateState.PROMOTED
        created += int(is_new)
        updated += int(not is_new)
        mark = "+" if is_new else "~"
        print(
            f"  {mark} #{candidate.pr_number:<6} {task.task_id:<26} "
            f"{task.difficulty.value:<7} F2P {len(task.fail_to_pass):>2} "
            f"P2P {len(task.pass_to_pass):>5}"
        )

    print(f"\n新建 {created}，更新 {updated}，失败 {failed}")
    if args.dry_run:
        print("（--dry-run：没有写库）")
    else:
        print(
            "\n下一步：\n"
            "  1. uv run python -m cli.images build --env <环境>   # 把镜像 digest 写回库\n"
            "  2. uv run python -m cli.validate run               # 正式跑八步验证"
        )
    return 1 if failed else 0


# ══════════════════════════════════════════════════════════════
# show
# ══════════════════════════════════════════════════════════════


def cmd_show(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        rows = session.execute(
            sa.select(Repository.full_name, TaskCandidate.pr_number, TaskCandidate.raw_payload)
            .join(Repository, Repository.id == TaskCandidate.repository_id)
            .order_by(TaskCandidate.pr_number)
        ).all()

    printed = 0
    print(f"{'PR':>7}  {'分支':<8} {'预筛':<7} {'探测':<18} {'P2P':>6}  说明")
    for _repo, pr, payload in rows:
        probe = (payload or {}).get("probe")
        if not probe:
            continue
        if args.failed_only and probe.get("state") == "OK":
            continue
        pr_meta = (payload or {}).get("pr") or {}
        prescreen = (payload or {}).get("prescreen") or {}
        state = probe.get("reason_code") or probe.get("state") or "—"
        p2p = probe.get("p2p_count")
        print(
            f"{pr:>7}  {pr_meta.get('base_ref_name') or '—'!s:<8} "
            f"{prescreen.get('decision') or '—'!s:<7} {state:<18} "
            f"{p2p if p2p is not None else '—':>6}  {str(probe.get('detail') or '')[:80]}"
        )
        printed += 1
    if not printed:
        print("（还没有探测结果，先跑 `python -m cli.promote probe`）")
    return 0


# ══════════════════════════════════════════════════════════════
# 人工终审（§8.4 最后一步、§8.1 给 L1 写的"人工终审"）
# ══════════════════════════════════════════════════════════════

#: 人工终审的两个判定。
VERDICTS = ("ACCEPT", "REJECT")

#: 导出给人看的 CSV 表头。`verdict` 和 `reason` 留空，人填完再导回来。
#:
#: **`fail_to_pass` 和 `gold_patch` 必须在表里**，这是 2026-09-10 第一轮终审的教训：
#: 只读题面判不出"题面够不够支撑测试要求"。实测有三道题题面读着挺干净，
#: 但对着 F2P 清单一看，测试要求的行为题面里根本没提（#2796 要求支持自定义类型、
#: #3391 要求 `capture="fd"/"sys"`、#3473 要求 `"Positional arguments:"` 这个标题
#: 和它排在 `"Options:"` 前面）—— 被测 AI 就算完全照题面做也过不了。
#: 反过来对着 gold 补丁看，才认得出"题面里那句大白话就是官方的改法"（#2800、#3299）。
#:
#: gold_patch 进这张表不算泄题：它是给复审人看的，从来不下发给被测 AI（协议 C-44），
#: 而且题目 JSON 里本来就有一份。
REVIEW_COLUMNS = (
    "task_id",
    "pr",
    "branch",
    "prescreen_decision",
    "prescreen_score",
    "prescreen_leaks_fix",
    "validation_state",
    "difficulty",
    "f2p_count",
    "p2p_count",
    "issue_title",
    "issue_body",
    "fail_to_pass",
    "gold_patch",
    "verdict",
    "reason",
)


def _review_rows(session: Session, dataset_id: str) -> list[dict[str, Any]]:
    """要人看的题：验证活下来的那些。

    **顺序是有意的：先验证、后人工。** 反过来的话，人要看的是全部候选，
    而其中一大半会被验证淘汰，白看。§8.4 把人工队列放在验证之后也是这个意思。
    """
    rows = session.execute(
        sa.select(BenchmarkTask, TaskCandidate.raw_payload)
        .join(Repository, Repository.id == BenchmarkTask.repository_id)
        .outerjoin(
            TaskCandidate,
            sa.and_(
                TaskCandidate.repository_id == BenchmarkTask.repository_id,
                sa.cast(TaskCandidate.pr_number, sa.String)
                == sa.func.split_part(BenchmarkTask.task_id, "-", 2),
            ),
        )
        .where(
            BenchmarkTask.validation_state.in_(
                [TaskValidationState.VALID, TaskValidationState.REVIEW_REQUIRED]
            )
        )
        .order_by(BenchmarkTask.task_id)
    ).all()

    out: list[dict[str, Any]] = []
    for task, payload in rows:
        if (task.raw_definition or {}).get("dataset_id") != dataset_id:
            continue
        prescreen = ((payload or {}).get("prescreen") or {}) if payload else {}
        pr_meta = ((payload or {}).get("pr") or {}) if payload else {}
        out.append(
            {
                "task_id": task.task_id,
                "pr": task.task_id.rsplit("-", 1)[-1],
                "branch": pr_meta.get("base_ref_name") or "",
                "prescreen_decision": prescreen.get("decision") or "",
                "prescreen_score": prescreen.get("score") or "",
                "prescreen_leaks_fix": prescreen.get("leaks_fix"),
                "validation_state": task.validation_state.value,
                "difficulty": task.difficulty.value,
                "f2p_count": len(task.fail_to_pass),
                "p2p_count": len(task.pass_to_pass),
                "issue_title": task.issue_title,
                "issue_body": task.issue_body,
                "fail_to_pass": "\n".join(task.fail_to_pass),
                "gold_patch": (task.raw_definition or {}).get("gold_patch", ""),
                "verdict": "",
                "reason": "",
            }
        )
    return out


def cmd_export_review(args: argparse.Namespace) -> int:
    """导出人工终审对照表。

    **为什么 PASS 那一档也要过人眼**：E1-T5 实测 PASS 的假通过率 2/11 ≈ 18%
    （§8.10 第十节），而**验证流水线对泄题完全无感** —— 题面里写着修复方案的题，
    八步会全过、判 VALID，它甚至更容易过，因为 gold 一定修得好。
    这 18% 一条都不会被验证挡掉，所以 §8.1 给 L1 写的"人工终审"必须真的做。
    """
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        rows = _review_rows(session, args.dataset_id)
    if not rows:
        print("没有要复审的题（先跑 assemble + validate）", file=sys.stderr)
        return 1

    out = (
        Path(args.out)
        if args.out
        else (FUNNEL_ROOT / f"review-{datetime.now(UTC).strftime('%Y-%m-%d')}.csv")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        # utf-8-sig：Excel 不认没有 BOM 的 UTF-8，中文题面会变乱码。
        # lineterminator 要显式给 \n：csv 模块默认写 \r\n，而仓库的 pre-commit
        # 有一条 mixed-line-ending 钩子，不给的话每次导出都会被它改一遍
        writer = csv.DictWriter(handle, fieldnames=list(REVIEW_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    print(f"导出 {len(rows)} 条到 {out}")
    print(f"填 verdict 列（{'/'.join(VERDICTS)}）和 reason 列，然后：")
    print(f"  uv run python -m cli.promote import-review {out}")
    print("\n看四件事，**后两件必须对着 fail_to_pass 和 gold_patch 看**：")
    print("  1. 题面有没有把修复方案说出来（含用大白话说的，正则查不到）")
    print("  2. 题面够不够自足（原因是不是写在别的 issue 里）")
    print("  3. 题面**够不够支撑 F2P 的要求** —— 测试要的行为题面里提了吗")
    print("  4. 和别的题是不是同一个 issue（重复题会让同一个问题算两次）")
    return 0


def next_state(current: TaskValidationState, verdict: str) -> TaskValidationState:
    """人工终审判完之后题目该是什么状态（§7.4 的 `REVIEW_REQUIRED →（人工）→ VALID / INVALID`）。

    单独抽出来是因为**这里漏过一条边**：第一版只写了 `REJECT → INVALID`，
    `ACCEPT` 那一侧原样不动，于是被人看过并且收下的题永远卡在 `REVIEW_REQUIRED`，
    而 E1-T6 按 `VALID` 挑题 —— 它会静悄悄地掉出数据集，不报错。
    """
    if verdict == "REJECT":
        return TaskValidationState.INVALID
    if current is TaskValidationState.REVIEW_REQUIRED:
        return TaskValidationState.VALID
    return current


def cmd_import_review(args: argparse.Namespace) -> int:
    """把填好的对照表导回库。

    §7.4 的状态机是 `REVIEW_REQUIRED →（人工）→ VALID / INVALID`，**两条边都要走**：

    - `REJECT` → `INVALID`。**不写 `invalid_reason_code`**：§7.3 那七个 code 说的都是
      "跑出来不合格"，而人工否掉的理由是题面问题，硬套一个是在编（§7.10 立过这条规矩）。
    - `ACCEPT` 且题目停在 `REVIEW_REQUIRED` → `VALID`。少了这条边，被人看过并且收下的题
      会**永远卡在 `REVIEW_REQUIRED`**，而 E1-T6 按 `VALID` 挑题 —— 它会静悄悄地掉出数据集。
      2026-09-10 第一次导入就漏了这条：收下 21 条，库里只有 20 道 VALID。
    - `ACCEPT` 且题目本来就是 `VALID` → 不动。

    理由原文一律记进候选的 `raw_payload.final_review`，包括收下的那些 ——
    "为什么这道带复核旗子的题还是收了"是要能回答的。
    """
    path = Path(args.file)
    if not path.exists():
        print(f"文件不在：{path}", file=sys.stderr)
        return 1

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if (r.get("verdict") or "").strip()]
    if not rows:
        print("表里一条 verdict 都没填", file=sys.stderr)
        return 1

    bad = [r["task_id"] for r in rows if r["verdict"].strip().upper() not in VERDICTS]
    if bad:
        print(f"verdict 只能填 {'/'.join(VERDICTS)}，这些填错了：{bad}", file=sys.stderr)
        return 1

    engine = create_db_engine()
    factory = create_session_factory(engine)
    accepted = rejected = promoted = 0
    now = datetime.now(UTC).isoformat()

    with session_scope(factory) as session:
        for row in rows:
            task_id = row["task_id"].strip()
            verdict = row["verdict"].strip().upper()
            reason = (row.get("reason") or "").strip()

            task = session.execute(
                sa.select(BenchmarkTask).where(BenchmarkTask.task_id == task_id)
            ).scalar_one_or_none()
            if task is None:
                print(f"  ? {task_id} 库里没有，跳过")
                continue

            pr_number = int(task_id.rsplit("-", 1)[-1])
            candidate = session.execute(
                sa.select(TaskCandidate).where(
                    TaskCandidate.repository_id == task.repository_id,
                    TaskCandidate.pr_number == pr_number,
                )
            ).scalar_one_or_none()
            if candidate is not None:
                payload = dict(candidate.raw_payload)
                payload["final_review"] = {
                    "reviewed_at": now,
                    "reviewer": args.reviewer,
                    "verdict": verdict,
                    "reason": reason,
                }
                candidate.raw_payload = payload

            was = task.validation_state
            task.validation_state = next_state(was, verdict)
            if verdict == "REJECT":
                rejected += 1
            else:
                accepted += 1
                promoted += int(was is TaskValidationState.REVIEW_REQUIRED)

    print(
        f"收下 {accepted} 条（其中 {promoted} 条从 REVIEW_REQUIRED 转成 VALID），否掉 {rejected} 条"
    )
    return 0


# ══════════════════════════════════════════════════════════════
# report：漏斗
# ══════════════════════════════════════════════════════════════

#: 漏斗存档落在这儿，进版本库（KB 级，是 E8-T5 数据集质量报告的原料）。
FUNNEL_ROOT = REPO_ROOT / "datasets" / "benchmark-dev"


def _funnel(session: Session, dataset_id: str) -> dict[str, Any]:
    """从库里数出每一层剩多少、掉队的是为什么。

    每一层都带上"掉队原因的分类计数" —— 只报一个总数的话，
    题库产出率低了没人说得清是该改挖掘、改预筛，还是改环境。
    """
    from collections import Counter

    rows = session.execute(
        sa.select(TaskCandidate.state, TaskCandidate.raw_payload).join(
            Repository, Repository.id == TaskCandidate.repository_id
        )
    ).all()

    prescreen: Counter[str] = Counter()
    no_f2p = 0
    probe: Counter[str] = Counter()
    review: Counter[str] = Counter()
    for _state, payload in rows:
        final = ((payload or {}).get("final_review") or {}) if payload else {}
        if final.get("verdict"):
            review[str(final["verdict"])] += 1
        decision = str(((payload or {}).get("prescreen") or {}).get("decision") or "—")
        prescreen[decision] += 1
        if decision not in PROMOTABLE_DECISIONS:
            continue
        if not ((payload or {}).get("cleaned") or {}).get("f2p_candidates"):
            no_f2p += 1
            continue
        probe[str(((payload or {}).get("probe") or {}).get("state") or "未探测")] += 1

    tasks = session.execute(
        sa.select(
            BenchmarkTask.task_id,
            BenchmarkTask.difficulty,
            BenchmarkTask.validation_state,
            BenchmarkTask.fail_to_pass,
            BenchmarkTask.pass_to_pass,
            BenchmarkTask.raw_definition,
        )
    ).all()
    mine = [t for t in tasks if (t.raw_definition or {}).get("dataset_id") == dataset_id]

    # 难度和用例数只算**留下来的那些**。算上被否掉的会让分布图说谎 ——
    # 2026-09-10 就差点这样：唯一一道 easy 恰好是人工终审否掉的那条
    kept = [t for t in mine if t.validation_state is TaskValidationState.VALID]

    return {
        "dataset_id": dataset_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidates_total": len(rows),
        "prescreen": dict(prescreen.most_common()),
        "no_f2p_candidate": no_f2p,
        "probe": dict(probe.most_common()),
        "final_review": dict(review.most_common()),
        "tasks_total": len(mine),
        "validation": dict(Counter(t.validation_state.value for t in mine).most_common()),
        "kept_total": len(kept),
        "difficulty": dict(Counter(t.difficulty.value for t in kept).most_common()),
        "f2p_total": sum(len(t.fail_to_pass) for t in kept),
        "p2p_total": sum(len(t.pass_to_pass) for t in kept),
    }


def _render_funnel(data: dict[str, Any]) -> str:
    lines = [f"数据集 {data['dataset_id']}（{data['generated_at'][:19]}）", ""]
    lines.append(f"候选总数              {data['candidates_total']}")
    for name, count in data["prescreen"].items():
        lines.append(f"  预筛 {name:<16} {count}")
    lines.append(f"  抽不出候选 F2P       {data['no_f2p_candidate']}（进不了探测）")
    lines.append("")
    lines.append("探测轮")
    for name, count in data["probe"].items():
        lines.append(f"  {name:<22} {count}")
    if data.get("final_review"):
        lines.append("")
        lines.append("人工终审")
        for name, count in data["final_review"].items():
            lines.append(f"  {name:<22} {count}")
    lines.append("")
    lines.append(f"入库题目              {data['tasks_total']}")
    for name, count in data["validation"].items():
        lines.append(f"  {name:<22} {count}")
    lines.append("")
    lines.append(f"数据集定档            {data['kept_total']} 道（VALID）")
    for name, count in data["difficulty"].items():
        lines.append(f"  难度 {name:<18} {count}")
    if data["kept_total"]:
        lines.append(f"  F2P 合计 {data['f2p_total']}，P2P 合计 {data['p2p_total']}")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    engine = create_db_engine()
    with session_scope(create_session_factory(engine)) as session:
        data = _funnel(session, args.dataset_id)

    print(_render_funnel(data))
    if args.save:
        FUNNEL_ROOT.mkdir(parents=True, exist_ok=True)
        path = FUNNEL_ROOT / f"funnel-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n存档：{path.relative_to(REPO_ROOT)}")
    return 0


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.promote", description="把挖掘候选推成题目（E8-T2）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="探测轮：实测证伪 F2P、派生 P2P（要 Docker）")
    probe.add_argument("--repo", help="只探这个仓库，例如 pallets/click")
    probe.add_argument("--pr", type=int, action="append", help="只探这些 PR，可重复")
    probe.add_argument(
        "--decision",
        action="append",
        choices=list(PROMOTABLE_DECISIONS),
        help=f"只探这些预筛分档，默认 {'/'.join(PROMOTABLE_DECISIONS)}",
    )
    probe.add_argument("--branch", help="只探合进这个分支的 PR，例如 stable")
    probe.add_argument("--limit", type=int, help="最多探几条")
    probe.add_argument("--redo", action="store_true", help="已经探过的也重探")
    probe.add_argument("--dry-run", action="store_true", help="不写库")
    probe.set_defaults(func=cmd_probe)

    build = sub.add_parser("assemble", help="把探测通过的候选组装成题目写进库")
    build.add_argument("--repo", help="只做这个仓库")
    build.add_argument("--pr", type=int, action="append", help="只做这些 PR，可重复")
    build.add_argument(
        "--limit",
        type=int,
        help="数据集最终要有多少道（不是这一次加多少）；要补的多于候选时按 PR 号等距抽",
    )
    build.add_argument("--dataset-id", default=DEFAULT_DATASET_ID, help="题目的 dataset_id")
    build.add_argument("--dry-run", action="store_true", help="不写库")
    build.add_argument(
        "--redo",
        action="store_true",
        help="连已经入过库的候选一起重做（派生规则改了要用，会重算 content_hash）",
    )
    build.set_defaults(func=cmd_assemble)

    show = sub.add_parser("show", help="看探测结果")
    show.add_argument("--failed-only", action="store_true", help="只看没过的")
    show.set_defaults(func=cmd_show)

    export = sub.add_parser("export-review", help="导出人工终审对照表（CSV）")
    export.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    export.add_argument("--out", help="写到哪，默认 datasets/benchmark-dev/review-<日期>.csv")
    export.set_defaults(func=cmd_export_review)

    imp = sub.add_parser("import-review", help="把填好的对照表导回库")
    imp.add_argument("file", help="填好 verdict 列的 CSV")
    imp.add_argument("--reviewer", default="human", help="复审人，记进证据")
    imp.set_defaults(func=cmd_import_review)

    report = sub.add_parser("report", help="漏斗报表：每一层剩多少、掉队的为什么")
    report.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    report.add_argument("--save", action="store_true", help="存一份到 datasets/benchmark-dev/")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
