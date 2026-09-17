"""数据集质量报告（E8-T5）：来源构成 / 语言分布 / 难度分布 / 漏斗，从库里数出来。

    python -m cli.quality report                       # 默认看两套主数据集的最新已发布版本
    python -m cli.quality report --set benchmark-cn-v1@v1 --set swebench-verified-subset@v2
    python -m cli.quality report --save                # 落 datasets/quality/quality-<日期>.md/.json

## 数的是"发布版里的题"，不是"库里 VALID 的题"

MET-05 数的是**可评测的题**，最终实验（E10-T4）也是从 `benchmark_set_items` 取题。
库里 VALID 但没冻进任何发布版的题，评测时一道都选不出来，报告里把它算进去就是虚报。
所以每一节都按 `benchmark_sets` → `benchmark_set_items` → `benchmark_tasks` 这条链数。

唯一的例外单独列出来：Golden 题（L0，`golden-v1`）全是中文，但它是测试基石、不在
发布版里 —— MET-05 底线第二句"自建中文题 ≥40"对账时得说清楚这 4 道算不算。

## 两条漏斗各有各的原料

自建题（`benchmark-dev` → `benchmark-cn-v1`）的漏斗从 `task_candidates.raw_payload`
里逐层数（预筛 / 候选 F2P / 探测 / 终审），按仓库分开 —— 只报一个总数的话，
产出率低了说不清是该换仓库、改预筛，还是改环境（E8-T3 第一段就是这么发现
sqlfluff 出不了题的）。官方题的漏斗 `cli.swebench report` 已经会算，这里直接复用，
原料文件（`var/cache/swebench/`）不在时如实写"本机没有"，不编数。

## 为什么放 `cli/` 不放 `app/`

要同时读 `app.benchmark`（数据集、候选）和 `cli.swebench`（官方漏斗），
`cli/` 是唯一的组合层，理由同 `cli/dataset.py`。
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.dataset import DatasetError, resolve_set
from app.benchmark.swebench_import import DATASET_ID as OFFICIAL_DATASET_ID
from app.benchmark.swebench_import import Funnel, render_funnel
from app.domain.enums import BenchmarkSetStatus, TaskValidationState
from app.infrastructure.config import REPO_ROOT
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import (
    BenchmarkSetItem,
    BenchmarkTask,
    Repository,
    TaskCandidate,
)

#: 报告落点，进版本库（KB 级）。
QUALITY_ROOT = REPO_ROOT / "datasets" / "quality"

#: 不给 `--set` 时看这两套：自建主数据集 + 官方校准集（§8.1 的 L2 和 L2'）。
DEFAULT_SLUGS: tuple[str, ...] = ("benchmark-cn-v1", OFFICIAL_DATASET_ID)

#: Golden 题的 dataset_id（L0）。它不在发布版里，但中文对账要提它。
GOLDEN_DATASET_ID = "golden-v1"

#: 预筛这两档才有资格进探测（同 `cli.promote.PROMOTABLE_DECISIONS`，这里不 import
#: 那个模块 —— 它一进来就把 Docker 那一串也带进来了，报告用不着）。
PROMOTABLE = ("PASS", "REVIEW")


# ══════════════════════════════════════════════════════════════
# 一个发布版的画像
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TaskRow:
    """报告用得着的那几列，从 `benchmark_tasks` 投影出来，测试里直接手造。"""

    task_id: str
    repo: str
    is_domestic: bool
    language: str
    difficulty: str
    f2p_count: int
    p2p_count: int
    agent_timeout_s: int
    test_timeout_s: int


@dataclass(slots=True)
class SetProfile:
    """一个数据集版本的来源 / 语言 / 难度 / 规模。"""

    slug: str
    version: str
    status: str
    source_dataset_id: str | None
    published_at: str | None
    snapshot_digest: str | None
    gate: dict[str, Any] | None
    tasks: list[TaskRow] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.slug}@{self.version}"

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    def by_repo(self) -> dict[str, int]:
        return dict(Counter(t.repo for t in self.tasks).most_common())

    def domestic_count(self) -> int:
        return sum(1 for t in self.tasks if t.is_domestic)

    def languages(self) -> dict[str, int]:
        return dict(Counter(t.language for t in self.tasks).most_common())

    def difficulty(self) -> dict[str, int]:
        return dict(Counter(t.difficulty for t in self.tasks).most_common())

    def timeouts(self) -> dict[str, dict[str, int]]:
        return {
            "agent_timeout_s": _int_counts(t.agent_timeout_s for t in self.tasks),
            "test_timeout_s": _int_counts(t.test_timeout_s for t in self.tasks),
        }

    def f2p(self) -> dict[str, float]:
        return _size_stats([t.f2p_count for t in self.tasks])

    def p2p(self) -> dict[str, float]:
        return _size_stats([t.p2p_count for t in self.tasks])

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "version": self.version,
            "status": self.status,
            "source_dataset_id": self.source_dataset_id,
            "published_at": self.published_at,
            "snapshot_digest": self.snapshot_digest,
            "gate": self.gate,
            "task_count": self.task_count,
            "repos": self.by_repo(),
            "domestic_count": self.domestic_count(),
            "languages": self.languages(),
            "difficulty": self.difficulty(),
            "f2p": self.f2p(),
            "p2p": self.p2p(),
            "timeouts": self.timeouts(),
            "task_ids": [t.task_id for t in self.tasks],
        }


def _int_counts(values: Iterable[int]) -> dict[str, int]:
    return {str(k): v for k, v in sorted(Counter(values).items())}


def _size_stats(values: Sequence[int]) -> dict[str, float]:
    """合计 / 中位数 / 最小 / 最大。中位数比平均数抗 P2P 那种几千条的长尾。"""
    if not values:
        return {"total": 0, "median": 0, "min": 0, "max": 0}
    return {
        "total": int(sum(values)),
        "median": float(statistics.median(values)),
        "min": int(min(values)),
        "max": int(max(values)),
    }


def load_profile(session: Session, slug: str, version: str | None) -> SetProfile:
    """按 slug（可带版本）取一个版本的画像。不带版本取最新已发布的；没发布的用草稿并写明。"""
    dataset, _note = resolve_set(session, slug, version)
    rows = session.execute(
        sa.select(
            BenchmarkTask.task_id,
            Repository.full_name,
            Repository.is_domestic,
            BenchmarkTask.issue_language,
            BenchmarkTask.difficulty,
            BenchmarkTask.fail_to_pass,
            BenchmarkTask.pass_to_pass,
            BenchmarkTask.agent_timeout_s,
            BenchmarkTask.test_timeout_s,
        )
        .join(BenchmarkSetItem, BenchmarkSetItem.benchmark_task_id == BenchmarkTask.id)
        .join(Repository, Repository.id == BenchmarkTask.repository_id)
        .where(BenchmarkSetItem.benchmark_set_id == dataset.id)
        .order_by(BenchmarkTask.task_id)
    ).all()
    tasks = [
        TaskRow(
            task_id=task_id,
            repo=repo,
            is_domestic=bool(domestic),
            language=language.value,
            difficulty=difficulty.value,
            f2p_count=len(f2p),
            p2p_count=len(p2p),
            agent_timeout_s=agent_timeout,
            test_timeout_s=test_timeout,
        )
        for (
            task_id,
            repo,
            domestic,
            language,
            difficulty,
            f2p,
            p2p,
            agent_timeout,
            test_timeout,
        ) in rows
    ]
    digest = dataset.snapshot_digest
    return SetProfile(
        slug=dataset.slug,
        version=dataset.version,
        status=dataset.status.value,
        source_dataset_id=dataset.source_dataset_id,
        published_at=dataset.published_at.isoformat() if dataset.published_at else None,
        snapshot_digest=f"sha256:{digest}" if digest else None,
        gate=dict(dataset.publish_evidence) if dataset.publish_evidence else None,
        tasks=tasks,
    )


def unpublished_chinese(session: Session, published_task_ids: set[str]) -> dict[str, int]:
    """库里 VALID、中文（zh / mixed）、但没冻进任何发布版的题，按 dataset_id 数。

    MET-05 底线第二句对账时要用：这些题**不算**"可评测的题"，
    但 Golden 那 4 道是我们自己写的中文题，报告里得交代它们在哪。
    """
    rows = session.execute(
        sa.select(BenchmarkTask.task_id, BenchmarkTask.raw_definition["dataset_id"].astext).where(
            BenchmarkTask.validation_state == TaskValidationState.VALID,
            BenchmarkTask.issue_language.in_(["zh", "mixed"]),
        )
    ).all()
    counts: Counter[str] = Counter()
    for task_id, dataset_id in rows:
        if task_id not in published_task_ids:
            counts[str(dataset_id or "?")] += 1
    return dict(sorted(counts.items()))


# ══════════════════════════════════════════════════════════════
# 自建题的漏斗（按仓库）
# ══════════════════════════════════════════════════════════════


@dataclass(slots=True)
class RepoFunnel:
    """一个仓库从候选到进集的每一层。字段顺序就是漏斗顺序。"""

    repo: str
    candidates: int = 0
    prescreen: Counter[str] = field(default_factory=Counter)
    promotable: int = 0
    with_f2p_candidate: int = 0
    probe: Counter[str] = field(default_factory=Counter)
    tasks: Counter[str] = field(default_factory=Counter)
    final_review: Counter[str] = field(default_factory=Counter)
    in_set: int = 0

    @property
    def probe_ok(self) -> int:
        return self.probe.get("OK", 0)

    @property
    def valid(self) -> int:
        return self.tasks.get("VALID", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "candidates": self.candidates,
            "prescreen": dict(self.prescreen.most_common()),
            "promotable": self.promotable,
            "with_f2p_candidate": self.with_f2p_candidate,
            "probe": dict(self.probe.most_common()),
            "tasks": dict(self.tasks.most_common()),
            "final_review": dict(self.final_review.most_common()),
            "in_set": self.in_set,
        }


def mined_funnel(
    candidates: Iterable[tuple[str, Mapping[str, Any] | None]],
    tasks: Iterable[tuple[str, str]],
    in_set: Iterable[str],
) -> list[RepoFunnel]:
    """从候选 payload、题目状态、进集题号数出每个仓库的漏斗。纯函数，测试直接喂元组。

    - `candidates`：(仓库, raw_payload)
    - `tasks`：(仓库, validation_state) —— 只喂自建那个 dataset_id 的题
    - `in_set`：进了发布版的题的仓库，一道一条
    """
    funnels: dict[str, RepoFunnel] = {}

    def get(repo: str) -> RepoFunnel:
        return funnels.setdefault(repo, RepoFunnel(repo=repo))

    for repo, payload in candidates:
        funnel = get(repo)
        funnel.candidates += 1
        body = dict(payload or {})
        decision = str((body.get("prescreen") or {}).get("decision") or "—")
        funnel.prescreen[decision] += 1
        verdict = (body.get("final_review") or {}).get("verdict")
        if verdict:
            funnel.final_review[str(verdict)] += 1
        if decision not in PROMOTABLE:
            continue
        funnel.promotable += 1
        if not (body.get("cleaned") or {}).get("f2p_candidates"):
            continue
        funnel.with_f2p_candidate += 1
        funnel.probe[str((body.get("probe") or {}).get("state") or "未探测")] += 1

    for repo, state in tasks:
        get(repo).tasks[state] += 1
    for repo in in_set:
        get(repo).in_set += 1

    return sorted(funnels.values(), key=lambda f: (-f.in_set, -f.candidates, f.repo))


def load_mined_funnel(
    session: Session, source_dataset_id: str, profile: SetProfile
) -> list[RepoFunnel]:
    candidates = session.execute(
        sa.select(Repository.full_name, TaskCandidate.raw_payload).join(
            Repository, Repository.id == TaskCandidate.repository_id
        )
    ).all()
    tasks = session.execute(
        sa.select(Repository.full_name, BenchmarkTask.validation_state)
        .join(Repository, Repository.id == BenchmarkTask.repository_id)
        .where(BenchmarkTask.raw_definition["dataset_id"].astext == source_dataset_id)
    ).all()
    return mined_funnel(
        [(repo, payload) for repo, payload in candidates],
        [(repo, state.value) for repo, state in tasks],
        [t.repo for t in profile.tasks],
    )


def load_official_funnel(session: Session) -> Funnel | None:
    """官方题的漏斗复用 `cli.swebench`；原料文件不在本机就返回 None。"""
    from cli import swebench as sw

    if not sw.ROWS_FILE.exists():
        return None
    from app.benchmark.swebench_import import DEFAULT_SAMPLE_SIZE, DEFAULT_SEED, screen_all

    instances = sw._load_all()
    path = sw.sample_path(DEFAULT_SEED, DEFAULT_SAMPLE_SIZE)
    sample = sw.load_sample(path) if path.exists() else None
    return sw.build_funnel(
        instances,
        screen_all(instances),
        sample,
        sw._read_json(sw.IMAGES_FILE),
        sw._read_json(sw.MIRRORS_FILE),
        sw._validation_states(session),
    )


# ══════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════


@dataclass(slots=True)
class QualityReport:
    generated_at: str
    profiles: list[SetProfile]
    unpublished_chinese: dict[str, int]
    mined: list[RepoFunnel]
    mined_source: str | None
    official: Funnel | None

    def totals(self) -> dict[str, Any]:
        tasks = [t for p in self.profiles for t in p.tasks]
        return {
            "task_count": len(tasks),
            "languages": dict(Counter(t.language for t in tasks).most_common()),
            "chinese_count": sum(1 for t in tasks if t.language in ("zh", "mixed")),
            "difficulty": dict(Counter(t.difficulty for t in tasks).most_common()),
            "domestic_count": sum(1 for t in tasks if t.is_domestic),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "sets": [p.to_dict() for p in self.profiles],
            "totals": self.totals(),
            "unpublished_chinese": self.unpublished_chinese,
            "mined_funnel": {
                "source_dataset_id": self.mined_source,
                "repos": [f.to_dict() for f in self.mined],
            },
            "official_funnel": _funnel_dict(self.official),
        }


def _funnel_dict(funnel: Funnel | None) -> dict[str, Any] | None:
    if funnel is None:
        return None
    return {
        "official_total": funnel.official_total,
        "offline": dict(funnel.offline),
        "pool": funnel.pool,
        "sampled": funnel.sampled,
        "image_pulled": funnel.image_pulled,
        "mirror_ready": funnel.mirror_ready,
        "imported": funnel.imported,
        "validation": dict(funnel.validation),
        "valid": funnel.valid,
        "per_repo": funnel.per_repo,
    }


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


def _gate_cell(gate: Mapping[str, Any] | None) -> str:
    if not gate:
        return "未发布（没过门禁）"
    oracle = gate.get("oracle") or {}
    noop = gate.get("noop") or {}
    return (
        f"Oracle #{oracle.get('evaluation_run_id')} {oracle.get('resolved_count')}/"
        f"{oracle.get('total_tasks')} · Noop #{noop.get('evaluation_run_id')} "
        f"{noop.get('resolved_count')}/{noop.get('total_tasks')}"
    )


def render_markdown(report: QualityReport) -> str:
    """整份报告，Markdown。数字全来自库，叙述留给 `docs/plan/`。"""
    totals = report.totals()
    lines: list[str] = [
        f"# 数据集质量报告（{report.generated_at[:10]}）",
        "",
        "数的是**发布版里的题**（`benchmark_set_items`），不是库里全部 VALID 的题。",
        "",
        "## 一、总览",
        "",
        "| 数据集版本 | 题数 | 来源 dataset_id | 状态 | 门禁（C-50） | 快照摘要 |",
        "|:---|---:|:---|:---|:---|:---|",
    ]
    for p in report.profiles:
        digest = (p.snapshot_digest or "")[:19] + ("…" if p.snapshot_digest else "")
        lines.append(
            f"| `{p.name}` | {p.task_count} | `{p.source_dataset_id or '—'}` | {p.status} "
            f"| {_gate_cell(p.gate)} | `{digest}` |"
        )
    lines.append(f"| **合计** | **{totals['task_count']}** | | | | |")

    lines += [
        "",
        "## 二、来源构成（按仓库）",
        "",
        "| 仓库 | 国产 | " + " | ".join(f"`{p.name}`" for p in report.profiles) + " | 小计 |",
        "|:---|:---:|" + "---:|" * len(report.profiles) + "---:|",
    ]
    repos: dict[str, dict[str, int]] = {}
    domestic: dict[str, bool] = {}
    for p in report.profiles:
        for t in p.tasks:
            repos.setdefault(t.repo, Counter())[p.name] += 1
            domestic[t.repo] = t.is_domestic
    for repo, counts in sorted(repos.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        cells = " | ".join(str(counts.get(p.name, 0)) for p in report.profiles)
        lines.append(
            f"| {repo} | {'是' if domestic[repo] else '否'} | {cells} | {sum(counts.values())} |"
        )
    lines.append(
        f"| **合计** | 国产 {totals['domestic_count']} | "
        + " | ".join(str(p.task_count) for p in report.profiles)
        + f" | **{totals['task_count']}** |"
    )

    lines += [
        "",
        "## 三、语言分布",
        "",
        "| 数据集版本 | " + " | ".join(("zh", "mixed", "en")) + " | 中文（zh+mixed） |",
        "|:---|---:|---:|---:|---:|",
    ]
    for p in report.profiles:
        langs = p.languages()
        zh, mixed = langs.get("zh", 0), langs.get("mixed", 0)
        lines.append(
            f"| `{p.name}` | {zh} | {mixed} | {langs.get('en', 0)} "
            f"| {zh + mixed}（{_pct(zh + mixed, p.task_count)}） |"
        )
    tl = totals["languages"]
    lines.append(
        f"| **合计** | {tl.get('zh', 0)} | {tl.get('mixed', 0)} | {tl.get('en', 0)} "
        f"| **{totals['chinese_count']}**"
        f"（{_pct(totals['chinese_count'], totals['task_count'])}） |"
    )
    if report.unpublished_chinese:
        extra = "、".join(f"`{k}` {v} 道" for k, v in report.unpublished_chinese.items())
        lines += [
            "",
            f"库里另有 VALID 的中文题**不在任何发布版里**：{extra}。"
            "它们不算「可评测的题」，上表没有计入。",
        ]

    lines += [
        "",
        "## 四、难度分布",
        "",
        "| 数据集版本 | easy | medium | hard |",
        "|:---|---:|---:|---:|",
    ]
    for p in report.profiles:
        d = p.difficulty()
        lines.append(
            f"| `{p.name}` | {d.get('easy', 0)} | {d.get('medium', 0)} | {d.get('hard', 0)} |"
        )
    td = totals["difficulty"]
    lines.append(
        f"| **合计** | {td.get('easy', 0)} | {td.get('medium', 0)} | {td.get('hard', 0)} |"
    )

    lines += [
        "",
        "## 五、规模（F2P / P2P 用例数）",
        "",
        "| 数据集版本 | F2P 合计 | F2P 中位 | F2P 最大 | P2P 合计 | P2P 中位 | P2P 最大 "
        "| agent_timeout_s | test_timeout_s |",
        "|:---|---:|---:|---:|---:|---:|---:|:---|:---|",
    ]
    for p in report.profiles:
        f2p, p2p, to = p.f2p(), p.p2p(), p.timeouts()
        lines.append(
            f"| `{p.name}` | {f2p['total']} | {f2p['median']:g} | {f2p['max']} "
            f"| {p2p['total']} | {p2p['median']:g} | {p2p['max']} "
            f"| {_counts_cell(to['agent_timeout_s'])} | {_counts_cell(to['test_timeout_s'])} |"
        )

    lines += ["", f"## 六、自建题漏斗（来源 `{report.mined_source or '—'}`，按仓库）", ""]
    if report.mined:
        lines += [
            "| 仓库 | 候选 | 预筛 PASS / REVIEW / REJECT | 抽得出候选 F2P | 探测 OK "
            "| 入库 | VALID | 终审 收 / 否 | 进集 |",
            "|:---|---:|:---|---:|---:|---:|---:|:---|---:|",
        ]
        for f in report.mined:
            ps = f.prescreen
            fr = f.final_review
            lines.append(
                f"| {f.repo} | {f.candidates} "
                f"| {ps.get('PASS', 0)} / {ps.get('REVIEW', 0)} / {ps.get('REJECT', 0)} "
                f"| {f.with_f2p_candidate} | {f.probe_ok} | {sum(f.tasks.values())} | {f.valid} "
                f"| {fr.get('ACCEPT', 0)} / {fr.get('REJECT', 0)} | {f.in_set} |"
            )
        lines += [
            "",
            "「抽得出候选 F2P」只数预筛 PASS / REVIEW 的；"
            "「探测 OK」是探测轮实测 F2P 在基线上真的挂、gold 打上真的过。",
        ]
    else:
        lines.append("库里没有候选记录。")

    lines += ["", "## 七、官方题漏斗（SWE-bench Verified）", ""]
    if report.official is not None:
        lines.append(render_funnel(report.official))
    else:
        lines.append(
            "本机没有官方数据的原料文件（`var/cache/swebench/`），"
            "漏斗见 `datasets/swebench/import-report-*.md`。"
        )
    return "\n".join(lines) + "\n"


def _counts_cell(counts: Mapping[str, int]) -> str:
    return "、".join(f"{k}×{v}" for k, v in counts.items()) or "—"


def _parse_set(spec: str) -> tuple[str, str | None]:
    slug, _, version = spec.partition("@")
    return slug, (version or None)


def build_report(session: Session, specs: Sequence[str]) -> QualityReport:
    profiles: list[SetProfile] = []
    for spec in specs:
        slug, version = _parse_set(spec)
        profiles.append(load_profile(session, slug, version))

    published_ids = {t.task_id for p in profiles for t in p.tasks}
    mined_profile = next(
        (p for p in profiles if p.source_dataset_id and p.source_dataset_id != OFFICIAL_DATASET_ID),
        None,
    )
    mined = (
        load_mined_funnel(session, mined_profile.source_dataset_id or "", mined_profile)
        if mined_profile is not None
        else []
    )
    official = (
        load_official_funnel(session)
        if any(p.source_dataset_id == OFFICIAL_DATASET_ID for p in profiles)
        else None
    )
    return QualityReport(
        generated_at=datetime.now(UTC).isoformat(),
        profiles=profiles,
        unpublished_chinese=unpublished_chinese(session, published_ids),
        mined=mined,
        mined_source=mined_profile.source_dataset_id if mined_profile else None,
        official=official,
    )


def cmd_report(args: argparse.Namespace) -> int:
    specs = list(args.set) if args.set else list(DEFAULT_SLUGS)
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        try:
            report = build_report(session, specs)
        except DatasetError as exc:
            print(exc)
            return 1
        for p in report.profiles:
            if p.status != BenchmarkSetStatus.PUBLISHED.value:
                print(f"⚠ {p.name} 的状态是 {p.status}，不是已发布版本，数字仅供参考\n")
        text = render_markdown(report)
        data = report.to_dict()

    print(text)
    if args.save:
        QUALITY_ROOT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        md = QUALITY_ROOT / f"quality-{stamp}.md"
        js = QUALITY_ROOT / f"quality-{stamp}.json"
        md.write_text(text, encoding="utf-8")
        js.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"存档：{md.relative_to(REPO_ROOT)}、{js.relative_to(REPO_ROOT)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.quality", description="数据集质量报告（E8-T5）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="来源 / 语言 / 难度 / 漏斗，Markdown + JSON")
    report.add_argument(
        "--set",
        action="append",
        help=f"看哪个版本，slug 或 slug@version，可重复；默认 {' + '.join(DEFAULT_SLUGS)}",
    )
    report.add_argument(
        "--save", action="store_true", help=f"落 {QUALITY_ROOT.relative_to(REPO_ROOT)}/"
    )
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
