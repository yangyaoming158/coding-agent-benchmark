"""候选清洗与 LLM 预筛的命令行（E1-T5，`03-benchmark-spec.md` §8.4）。

    python -m cli.prescreen clean                 # 脱敏 + 拆补丁 + 抽候选 F2P（不花钱）
    python -m cli.prescreen score --limit 10      # LLM 打分（**花钱**，先跑 10 条看看）
    python -m cli.prescreen score                 # 全量打分
    python -m cli.prescreen report                # 分数分布与漏斗
    python -m cli.prescreen export-review         # 导出人工核对对照表

`make prescreen-clean` / `make prescreen` 是前两个的快捷方式。

## 为什么分成 clean 和 score 两段

**clean 不花钱，score 花钱。** 和 `cli.survey` 的 probe/measure 一个道理：
便宜的那段可以随便重跑、随便改规则；贵的那段要能单独控制批量。

clean 的产出（脱敏后的正文、劈好的补丁、候选 F2P）落在 `raw_payload` 和
`var/mining/patches/` 下，score 从那里读，不重新取 diff。

## 它做到哪一步为止

终点是把候选从 `DISCOVERED` 推到 `PRESCREENED` 或 `REJECTED`。

**不把候选变成题目。** 那是 E8-T2 的活，理由很硬：一道题必须有
`pass_to_pass`，而 §7.10 写死了"P2P 候选池来自验证流水线 S4 的全量报告"——
也就是说 P2P 只能靠真跑一遍测试得出来，而这条命令一个容器都不起。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.cleaning import (
    CLEANER_VERSION,
    extract_f2p_candidates,
    issue_text,
    leaks_remaining,
    redact_issue,
    split_diff,
)
from app.benchmark.gh_cache import CacheStats, cached_rest_text
from app.benchmark.prescreen import (
    PROMPT_VERSION,
    PrescreenInput,
    PrescreenVerdict,
    parse_verdict,
    rule_flags,
    score_histogram,
)
from app.benchmark.survey import detect_language
from app.domain.enums import TaskCandidateState
from app.infrastructure.config import REPO_ROOT
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.file_cache import DEFAULT_TTL_S, NO_CACHE
from app.infrastructure.llm import LLMClient, LLMConfigError, LLMError
from app.infrastructure.models.benchmark import Repository, TaskCandidate
from cli.survey import short

#: 劈好的补丁落这儿。**不进 `raw_payload`**：一个 PR 的 diff 动辄几十上百 KB，
#: 塞进 JSONB 会让一张几千行的表很难查。这些文件随时能从 GitHub 缓存重新生成，
#: 所以放在 `var/` 下、不进版本库。
PATCH_DIR = REPO_ROOT / "var" / "mining" / "patches"

#: 人工核对对照表落这儿。**进版本库**：AC 那条"抽 20 条人工核对一致率 ≥80%"
#: 要留下可复核的证据，和 `datasets/mining/` 一个待遇。
REVIEW_DIR = REPO_ROOT / "datasets" / "prescreen"

#: 抽多少条给人工核对。AC 写的就是 20。
DEFAULT_REVIEW_SAMPLE = 20
#: 抽样种子。固定住，这样"这 20 条"任何时候都能重现（和 §7.7 的 P2P 抽样同理）。
REVIEW_SEED = 20260909


def patch_path(repo: str, pr_number: int, kind: str) -> Path:
    return PATCH_DIR / repo.replace("/", "__") / f"{pr_number}.{kind}.patch"


# ══════════════════════════════════════════════════════════════
# clean：脱敏 + 拆补丁 + 抽候选 F2P
# ══════════════════════════════════════════════════════════════


@dataclass
class CleanCounts:
    """clean 这一段的漏斗。"""

    seen: int = 0
    cleaned: int = 0
    no_diff: int = 0
    unsplittable: int = 0
    leaked: int = 0
    redactions: int = 0

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def fetch_diff(repo: str, pr_number: int, *, ttl_s: int, stats: CacheStats) -> str:
    """取一个 PR 的 diff，走 E1-T4 那个文件缓存。

    用 `gh api` 的 diff 媒体类型，不从本地 git 取：本地取要先有镜像，
    而这一步本来就在打 GitHub API。缓存之后重跑不再花配额。
    """
    return cached_rest_text(
        f"repos/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
        ttl_s=ttl_s,
        stats=stats,
    )


def clean_candidate(
    payload: dict[str, Any], *, repo: str, diff: str
) -> tuple[dict[str, Any], list[str]]:
    """一条候选的全部清洗结果。返回 `(要合并进 raw_payload 的东西, 还残留的泄题形式)`。

    **原文一个字不改。** `raw_payload["issues"]` 里 E1-T4 抄下来的正文原样留着，
    脱敏结果写进新的 `cleaned` 段 —— 这样 E1-T5 自己脱干净没有，
    随时能把两份摆在一起对。覆盖掉原文的话，这个核对就再也做不了了。
    """
    title, body = issue_text(payload.get("issues") or [])
    redacted = redact_issue(body, repo=repo)
    redacted_title = redact_issue(title, repo=repo)
    split = split_diff(diff)
    f2p = extract_f2p_candidates(split.test_patch)
    leaks = leaks_remaining(f"{redacted_title.text}\n{redacted.text}", repo=repo)

    return {
        "cleaner_version": CLEANER_VERSION,
        "cleaned_at": datetime.now(UTC).isoformat(),
        "issue_title": redacted_title.text,
        "issue_body": redacted.text,
        "issue_language": detect_language(f"{redacted_title.text}\n{redacted.text}").value,
        "redaction": {
            "title": redacted_title.to_json(),
            "body": redacted.to_json(),
            "leaks_remaining": leaks,
        },
        "patches": split.to_json(),
        **f2p.to_json(),
    }, leaks


def cmd_clean(args: argparse.Namespace) -> int:
    ttl_s = NO_CACHE if args.no_cache else DEFAULT_TTL_S
    stats = CacheStats()
    counts = CleanCounts()

    engine = create_db_engine()
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = _load(session, args, states=(TaskCandidateState.DISCOVERED,))
            print(f"要清洗 {len(rows)} 条候选")
            for repo, row_id, pr_number, payload in rows:
                counts.seen += 1
                if payload.get("cleaned") and not args.force:
                    continue
                try:
                    diff = fetch_diff(repo, pr_number, ttl_s=ttl_s, stats=stats)
                # 一条失败不该拖垮整批 —— 取不到 diff 是常事（PR 太大、分支被删）
                except Exception as exc:
                    counts.no_diff += 1
                    print(f"  ! {repo}#{pr_number} 取不到 diff：{exc}", file=sys.stderr)
                    continue

                cleaned, leaks = clean_candidate(payload, repo=repo, diff=diff)
                counts.redactions += int(cleaned["redaction"]["body"]["total"])
                if leaks:
                    counts.leaked += 1
                if not cleaned["patches"]["usable"]:
                    counts.unsplittable += 1

                _write_patches(repo, pr_number, diff, cleaned)
                session.execute(
                    sa.update(TaskCandidate)
                    .where(TaskCandidate.id == row_id)
                    .values(raw_payload={**payload, "cleaned": cleaned})
                )
                counts.cleaned += 1
                if counts.cleaned % 10 == 0:
                    print(f"  已清洗 {counts.cleaned} 条…", flush=True)
    finally:
        engine.dispose()

    print(
        f"\n清洗完成：{counts.cleaned} 条\n"
        f"  取不到 diff  {counts.no_diff}\n"
        f"  劈不出补丁    {counts.unsplittable}（测试或源码那半是空的）\n"
        f"  脱敏后仍有泄题 {counts.leaked}\n"
        f"  共剥掉 {counts.redactions} 处（链接 / 哈希 / 补丁块）\n"
        f"  GitHub 缓存命中 {stats.hits}/{stats.total}"
    )
    return 1 if counts.leaked else 0


def _write_patches(repo: str, pr_number: int, diff: str, cleaned: dict[str, Any]) -> None:
    """劈好的两份补丁落盘，原始 diff 也留一份。

    留原始 diff 是为了能事后复核"劈得对不对"——只留劈完的，
    劈错了就再也看不出错在哪。它在 `var/` 下，不占版本库。
    """
    split = split_diff(diff)
    for kind, text in (("full", diff), ("test", split.test_patch), ("code", split.code_patch)):
        path = patch_path(repo, pr_number, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cleaned["patch_dir"] = short(patch_path(repo, pr_number, "full").parent)


# ══════════════════════════════════════════════════════════════
# score：LLM 打分
# ══════════════════════════════════════════════════════════════


def build_input(repo: str, pr_number: int, payload: dict[str, Any]) -> PrescreenInput:
    cleaned = payload.get("cleaned") or {}
    code_patch = ""
    path = patch_path(repo, pr_number, "code")
    if path.exists():
        code_patch = path.read_text(encoding="utf-8")
    return PrescreenInput(
        repo=repo,
        pr_number=pr_number,
        title=str(cleaned.get("issue_title") or ""),
        body=str(cleaned.get("issue_body") or ""),
        code_paths=tuple((cleaned.get("patches") or {}).get("code_paths") or []),
        code_patch=code_patch,
    )


def score_candidate(
    client: LLMClient, repo: str, pr_number: int, payload: dict[str, Any], *, refresh: bool
) -> PrescreenVerdict:
    """问一次模型，拿一个结论回来。"""
    cleaned = payload.get("cleaned") or {}
    flags = rule_flags(
        body=str(cleaned.get("issue_body") or ""),
        f2p_candidates=int(cleaned.get("count") or 0),
        leaks=list((cleaned.get("redaction") or {}).get("leaks_remaining") or []),
    )
    prompt = build_input(repo, pr_number, payload)
    response = client.complete(
        prompt.messages(),
        json_mode=True,
        cache_salt=prompt.cache_salt(),
        refresh=refresh,
    )
    return parse_verdict(response.json_payload(), flags=flags)


def cmd_score(args: argparse.Namespace) -> int:
    try:
        client = LLMClient.from_settings(
            model=args.model, ttl_s=NO_CACHE if args.no_cache else DEFAULT_TTL_S
        )
    except LLMConfigError as exc:
        print(f"没法调模型：{exc}", file=sys.stderr)
        return 1

    verdicts: list[tuple[str, int, PrescreenVerdict]] = []
    failures = 0
    engine = create_db_engine()
    factory = create_session_factory(engine)
    try:
        with client, session_scope(factory) as session:
            rows = _load(session, args, states=(TaskCandidateState.DISCOVERED,))
            rows = [r for r in rows if (r[3].get("cleaned"))]
            if args.limit:
                rows = rows[: args.limit]
            print(f"要打分 {len(rows)} 条候选，模型 {client.model}")
            if not rows:
                print("没有清洗过的候选。先跑 `python -m cli.prescreen clean`", file=sys.stderr)
                return 1

            for repo, row_id, pr_number, payload in rows:
                try:
                    verdict = score_candidate(
                        client, repo, pr_number, payload, refresh=args.refresh
                    )
                except (LLMError, ValueError) as exc:
                    failures += 1
                    print(f"  ! {repo}#{pr_number} 打分失败：{exc}", file=sys.stderr)
                    continue
                verdicts.append((repo, pr_number, verdict))
                if not args.dry_run:
                    _write_verdict(session, row_id, payload, verdict)
                print(
                    f"  {verdict.decision:6} {verdict.score:.0f} 分  {repo}#{pr_number}"
                    f"  {verdict.reason[:40]}",
                    flush=True,
                )
    finally:
        engine.dispose()

    print_scores(verdicts, client=client, failures=failures, dry_run=args.dry_run)
    archive = _write_score_archive(verdicts, client=client)
    print(f"  存档：{short(archive)}")
    return 0


def _write_verdict(
    session: Session, row_id: int, payload: dict[str, Any], verdict: PrescreenVerdict
) -> None:
    """结论落库。

    分数和理由进专门的列（`prescreen_score` / `prescreen_reason`），
    四个布尔量和标记进 `raw_payload` —— 列是给 SQL 查询用的投影，
    完整结论留在 JSONB 里，将来加一个维度不用改表。
    """
    state = (
        TaskCandidateState.REJECTED
        if verdict.decision == "REJECT"
        else TaskCandidateState.PRESCREENED
    )
    session.execute(
        sa.update(TaskCandidate)
        .where(TaskCandidate.id == row_id)
        .values(
            state=state,
            prescreen_score=verdict.score,
            prescreen_reason=verdict.reason[:500] or None,
            reject_reason=(verdict.reason[:500] or None)
            if state is TaskCandidateState.REJECTED
            else None,
            raw_payload={**payload, "prescreen": verdict.to_json()},
        )
    )


# ══════════════════════════════════════════════════════════════
# 报表
# ══════════════════════════════════════════════════════════════


def print_scores(
    verdicts: Sequence[tuple[str, int, PrescreenVerdict]],
    *,
    client: LLMClient | None = None,
    failures: int = 0,
    dry_run: bool = False,
) -> None:
    if not verdicts:
        print("一条都没打上分")
        return

    print("\n分数分布：")
    total = len(verdicts)
    for label, count in score_histogram([v.score for _, _, v in verdicts]):
        bar = "█" * round(40 * count / total) if count else ""
        print(f"  {label}  {count:>4}  {bar}")

    decisions: dict[str, int] = {}
    for _, _, v in verdicts:
        decisions[v.decision] = decisions.get(v.decision, 0) + 1
    print(
        "\n分流："
        + "  ".join(f"{k} {decisions.get(k, 0)}" for k in ("PASS", "REVIEW", "REJECT"))
        + f"（<{2:.0f} 分丢弃，<{4:.0f} 分或带标记进人工，其余通过；泄题一票否决）"
    )

    for name, pick in (
        ("泄题", lambda v: v.leaks_fix),
        ("过度指定", lambda v: v.over_specified),
        ("题面不自足", lambda v: not v.self_contained),
        ("定位不了", lambda v: not v.locatable),
    ):
        hits = [f"{r}#{n}" for r, n, v in verdicts if pick(v)]
        print(f"  {name}：{len(hits)}" + (f"（{', '.join(hits[:5])}…）" if hits else ""))

    flagged = [(r, n, v) for r, n, v in verdicts if v.flags]
    if flagged:
        print(f"\n规则标记（不问模型就能判的）：{len(flagged)} 条")
        for repo, number, v in flagged[:8]:
            print(f"  {repo}#{number}：{'；'.join(v.flags)}")

    if failures:
        print(f"\n打分失败 {failures} 条（见上面的 ! 行）")
    if client is not None:
        u = client.usage
        print(
            f"\n用量：问了 {u.calls} 次，其中 {u.cached_calls} 次命中缓存没花钱；"
            f"实际计费 {u.billed_calls} 次，"
            f"输入 {u.prompt_tokens} token、输出 {u.completion_tokens} token"
        )
    if dry_run:
        print("  --dry-run：没有写库")


def _write_score_archive(
    verdicts: Sequence[tuple[str, int, PrescreenVerdict]], *, client: LLMClient
) -> Path:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"scores-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "model": client.model,
                "prompt_version": PROMPT_VERSION,
                "usage": client.usage.to_json(),
                "histogram": score_histogram([v.score for _, _, v in verdicts]),
                "verdicts": [{"repo": r, "pr_number": n, **v.to_json()} for r, n, v in verdicts],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def cmd_report(args: argparse.Namespace) -> int:
    """从库里出报表。不联网、不调模型。"""
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            rows = session.execute(
                sa.select(
                    Repository.full_name,
                    TaskCandidate.pr_number,
                    TaskCandidate.state,
                    TaskCandidate.prescreen_score,
                    TaskCandidate.raw_payload,
                ).join(Repository, Repository.id == TaskCandidate.repository_id)
            ).all()
    finally:
        engine.dispose()

    if not rows:
        print("task_candidates 是空的。先跑 `python -m cli.mine run`")
        return 0

    cleaned = [r for r in rows if (r.raw_payload or {}).get("cleaned")]
    scored = [r for r in rows if (r.raw_payload or {}).get("prescreen")]
    print(f"候选 {len(rows)} 条：清洗过 {len(cleaned)}，打过分 {len(scored)}")

    if cleaned:
        leaks = [r for r in cleaned if (r.raw_payload["cleaned"]["redaction"]["leaks_remaining"])]
        unusable = [r for r in cleaned if not r.raw_payload["cleaned"]["patches"]["usable"]]
        f2p_counts = [int(r.raw_payload["cleaned"].get("count") or 0) for r in cleaned]
        no_f2p = sum(1 for c in f2p_counts if c == 0)
        print(
            f"\n清洗：脱敏后仍有泄题 {len(leaks)} 条；劈不出补丁 {len(unusable)} 条；"
            f"抽不出候选 F2P {no_f2p} 条\n"
            f"  候选 F2P 中位数 {sorted(f2p_counts)[len(f2p_counts) // 2]} 条，"
            f"最多 {max(f2p_counts)} 条"
        )

    if scored:
        verdicts = [
            (
                r.full_name,
                r.pr_number,
                parse_verdict(
                    r.raw_payload["prescreen"],
                    flags=tuple(r.raw_payload["prescreen"].get("flags") or []),
                ),
            )
            for r in scored
        ]
        print_scores(verdicts)
    return 0


# ══════════════════════════════════════════════════════════════
# export-review：人工核对对照表
# ══════════════════════════════════════════════════════════════


def cmd_export_review(args: argparse.Namespace) -> int:
    """抽 N 条导成 CSV，给人核对（AC：抽 20 条人工核对一致率 ≥80%）。

    **固定种子分层抽样**：按模型给的分档分层，每档都抽到 ——
    只按顺序取前 20 条的话，很可能全是同一个分档的，
    而"一致率"要问的恰恰是"各个分档判得准不准"。
    """
    import random

    engine = create_db_engine()
    try:
        with Session(engine) as session:
            rows = session.execute(
                sa.select(Repository.full_name, TaskCandidate.pr_number, TaskCandidate.raw_payload)
                .join(Repository, Repository.id == TaskCandidate.repository_id)
                .order_by(Repository.full_name, TaskCandidate.pr_number)
            ).all()
    finally:
        engine.dispose()

    scored = [r for r in rows if (r.raw_payload or {}).get("prescreen")]
    if not scored:
        print("还没有打过分的候选。先跑 `python -m cli.prescreen score`", file=sys.stderr)
        return 1

    by_score: dict[int, list[Any]] = {}
    for row in scored:
        by_score.setdefault(int(row.raw_payload["prescreen"]["score"]), []).append(row)

    rng = random.Random(args.seed)
    picked: list[Any] = []
    # 先每档抽一条保证覆盖，再按档的大小补足到 N
    for bucket in sorted(by_score):
        picked.append(rng.choice(by_score[bucket]))
    pool = [r for r in scored if r not in picked]
    rng.shuffle(pool)
    picked.extend(pool[: max(0, args.sample - len(picked))])
    picked = picked[: args.sample]

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"review-{datetime.now(UTC).strftime('%Y-%m-%d')}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "序号",
                "仓库",
                "PR",
                "issue 链接",
                "标题",
                "脱敏后正文（截断到 1200 字）",
                "改了哪些源码",
                "候选 F2P",
                "模型分数",
                "模型判定",
                "模型理由",
                "自足",
                "泄题",
                "过度指定",
                "可定位",
                "你的分数(0-5)",
                "你同意吗(y/n)",
                "备注",
            ]
        )
        for index, row in enumerate(picked, start=1):
            payload = row.raw_payload
            cleaned = payload.get("cleaned") or {}
            verdict = payload["prescreen"]
            issues = payload.get("issues") or [{}]
            writer.writerow(
                [
                    index,
                    row.full_name,
                    row.pr_number,
                    issues[0].get("url", ""),
                    cleaned.get("issue_title", ""),
                    str(cleaned.get("issue_body", ""))[:1200],
                    " ".join((cleaned.get("patches") or {}).get("code_paths") or []),
                    " ".join(cleaned.get("f2p_candidates") or []),
                    verdict["score"],
                    verdict["decision"],
                    verdict["reason"],
                    verdict["self_contained"],
                    verdict["leaks_fix"],
                    verdict["over_specified"],
                    verdict["locatable"],
                    "",
                    "",
                    "",
                ]
            )

    print(f"抽了 {len(picked)} 条，分层覆盖 {len(by_score)} 个分档，种子 {args.seed}")
    print(f"对照表：{short(path)}")
    print("\n填法：最后三列留给你 —— 「你的分数」按同样的 0~5 标准打，")
    print("「你同意吗」填 y/n（和模型判定 PASS/REVIEW/REJECT 是否一致），备注随意。")
    print("填完把一致率（y 的条数 / 总条数）告诉我，AC 要求 ≥80%。")
    return 0


# ══════════════════════════════════════════════════════════════
# 公共
# ══════════════════════════════════════════════════════════════


def _load(
    session: Session, args: argparse.Namespace, *, states: tuple[TaskCandidateState, ...]
) -> list[tuple[str, int, int, dict[str, Any]]]:
    """读候选，返回 `[(仓库全名, 行 id, PR 号, raw_payload)]`。"""
    query = (
        sa.select(
            Repository.full_name,
            TaskCandidate.id,
            TaskCandidate.pr_number,
            TaskCandidate.raw_payload,
        )
        .join(Repository, Repository.id == TaskCandidate.repository_id)
        .order_by(Repository.full_name, TaskCandidate.pr_number)
    )
    if not getattr(args, "all_states", False):
        query = query.where(TaskCandidate.state.in_(states))
    if getattr(args, "repo", None):
        query = query.where(Repository.full_name.in_(args.repo))
    if getattr(args, "pr", None):
        query = query.where(TaskCandidate.pr_number.in_(args.pr))
    return [(r[0], r[1], r[2], dict(r[3] or {})) for r in session.execute(query).all()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.prescreen", description="候选清洗与 LLM 预筛（E1-T5，§8.4）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_clean = sub.add_parser("clean", help="脱敏 + 拆补丁 + 抽候选 F2P（不花钱）")
    p_clean.add_argument("--repo", action="append", help="只清洗这几个仓库")
    p_clean.add_argument("--pr", action="append", type=int, help="只清洗这几个 PR")
    p_clean.add_argument("--force", action="store_true", help="已经清洗过的也重来")
    p_clean.add_argument("--no-cache", action="store_true", help="不用 GitHub 缓存")
    p_clean.add_argument("--all-states", action="store_true", help="连已预筛的一起清洗")
    p_clean.set_defaults(func=cmd_clean)

    p_score = sub.add_parser("score", help="LLM 打分（**要花钱**）")
    p_score.add_argument("--repo", action="append")
    p_score.add_argument("--pr", action="append", type=int)
    p_score.add_argument("--limit", type=int, help="只打前 N 条（先小批量试）")
    p_score.add_argument("--model", help="默认用 .env 里的 JUDGE_MODEL")
    p_score.add_argument("--refresh", action="store_true", help="忽略缓存重新问一遍")
    p_score.add_argument("--no-cache", action="store_true")
    p_score.add_argument("--dry-run", action="store_true", help="问模型但不写库")
    p_score.add_argument("--all-states", action="store_true")
    p_score.set_defaults(func=cmd_score)

    p_report = sub.add_parser("report", help="清洗与分数分布（读库，不联网）")
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser("export-review", help="导出人工核对对照表 CSV")
    p_export.add_argument("--sample", type=int, default=DEFAULT_REVIEW_SAMPLE)
    p_export.add_argument("--seed", type=int, default=REVIEW_SEED)
    p_export.set_defaults(func=cmd_export_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PATCH_DIR",
    "REVIEW_DIR",
    "CleanCounts",
    "build_input",
    "build_parser",
    "clean_candidate",
    "fetch_diff",
    "main",
    "patch_path",
    "score_candidate",
]
