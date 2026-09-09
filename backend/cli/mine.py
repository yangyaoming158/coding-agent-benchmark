"""GitHub 挖掘器的命令行（E1-T4，`03-benchmark-spec.md` §8.4）。

    python -m cli.mine run --repo pallets/click            # 挖一个仓库
    python -m cli.mine run --repo A --repo B --since 2024-09-09
    python -m cli.mine run --repo X --dry-run              # 不写库，只看产出率
    python -m cli.mine report                              # 候选产出率报表
    python -m cli.mine show                                # 库里现在有多少候选

`make mine` 是 `run` 的快捷方式。

## 它做到哪一步为止

**只负责"从 GitHub 抄下来"**：批量拉 merged PR + 关联 issue，按 §8.4 的三条
自动过滤刷一遍，剩下的原样写进 `task_candidates`（`state=DISCOVERED`）。

脱敏、拆 test_patch/code_patch、抽候选 F2P、LLM 预筛**全是 E1-T5 的活**。
理由见 `app/benchmark/mining.py` 的模块文档。

## 三条 AC 分别靠什么成立

| AC | 靠什么 |
|:---|:---|
| 重复运行不产生重复候选 | `UNIQUE(repository_id, pr_number)` 上的 upsert，不靠状态文件 |
| 限流下不崩、可续跑 | 每翻完一页就落盘（`var/mining/<repo>.json`）；配额低于阈值主动停 |
| 单仓库 ≥30 条候选 | 时间窗切分保证扫得全，见 `mining.split_window` |

再加一层：所有 GitHub 响应落文件缓存（`app/benchmark/gh_cache.py`），
所以就算进度文件丢了，重跑也是从缓存读，不花配额。

## 一个仓库出问题不丢前面所有结果

多仓库时每个仓库单独 try：第 3 个仓库限流失败，前 2 个的候选已经落库、
进度已经落盘，下次接着挖。这是 E8-T1 花过一次学费的地方
（`subprocess.TimeoutExpired` 没被包成 `GitHubError`，一次超时把整轮结果
连同烧掉的配额一起扔了）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.benchmark.gh_cache import (
    DEFAULT_TTL_S,
    CacheStats,
    cache_size,
    cached_graphql,
    cached_rest,
)
from app.benchmark.github import GitHubError, GitHubNotFoundError, gh_available
from app.benchmark.mining import (
    DEFAULT_BUCKETS,
    DEFAULT_MIN_QUOTA,
    DEFAULT_PAGE_SIZE,
    DEFAULT_WINDOW_DAYS,
    DEGRADED_BACKOFF_S,
    DEGRADED_RETRIES,
    PAGE_QUERY_TIMEOUT_S,
    PR_PAGE_QUERY,
    SEARCH_RESULT_CAP,
    MinedPR,
    MiningState,
    WindowState,
    bisect_window,
    build_payload,
    classify,
    default_since,
    funnel,
    new_state,
    parse_pr_node,
    search_query,
    summarize,
)
from app.benchmark.survey import RepoFacts
from app.domain.enums import TaskCandidateState
from app.infrastructure.config import REPO_ROOT
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.benchmark import Repository, TaskCandidate
from cli.survey import latest_archive, load_candidates, short

#: 断点续跑的进度文件放这儿。在 `var/` 下，不进版本库 ——
#: "翻到第几页了"是作业的私事，不是评测数据。
STATE_DIR = REPO_ROOT / "var" / "mining"

#: 产出率报表的存档放这儿。KB 级，**进版本库**，和 `datasets/survey/` 一个待遇：
#: 「这次挖掘扫了多少 PR、产出率多少」是选型和数据集构成的依据，要能事后复核。
REPORT_DIR = REPO_ROOT / "datasets" / "mining"


def state_path(repo: str) -> Path:
    return STATE_DIR / f"{repo.replace('/', '__')}.json"


def report_path(repo: str, stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d")
    return REPORT_DIR / f"{repo.replace('/', '__')}-{stamp}.json"


# ══════════════════════════════════════════════════════════════
# 进度文件
# ══════════════════════════════════════════════════════════════


def load_state(repo: str) -> MiningState | None:
    path = state_path(repo)
    if not path.exists():
        return None
    return MiningState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_state(state: MiningState) -> None:
    """落盘。**每翻完一页调一次** —— 这就是"可续跑"的全部实现。

    先写临时文件再 replace：Ctrl-C 正好打在写到一半时，
    半截 JSON 会让下次续跑直接崩在解析上。
    """
    state.updated_at = datetime.now(UTC).isoformat()
    path = state_path(state.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def resume_or_new(
    repo: str, *, since: date, until: date, buckets: int, page_size: int, restart: bool
) -> MiningState:
    """有进度就接着挖，没有就开新的。

    参数变了（时间范围、页大小）拒绝续跑：GitHub 的翻页游标是按页大小算偏移的，
    换了页大小接着走会**跳过一段 PR 而且不报错**。这种错查起来极难 ——
    表现只是"产出率莫名其妙比上次低"。所以宁可让人显式加 `--restart`。
    """
    existing = None if restart else load_state(repo)
    if existing is None:
        return new_state(repo, since=since, until=until, buckets=buckets, page_size=page_size)

    same = (
        existing.since == since.isoformat()
        and existing.until == until.isoformat()
        and existing.page_size == page_size
    )
    if not same:
        raise SystemExit(
            f"{repo} 已有进度（{existing.since}..{existing.until}，页大小 {existing.page_size}），"
            f"和这次的参数（{since}..{until}，页大小 {page_size}）对不上。\n"
            f"    要换参数请加 --restart（会从头挖，已落库的候选不受影响）"
        )
    return existing


# ══════════════════════════════════════════════════════════════
# 挖掘主循环
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PageResult:
    """翻完一页的产出，交给调用方落库。"""

    window: WindowState
    candidates: tuple[MinedPR, ...]
    from_cache: bool


def mine_repo(
    state: MiningState,
    *,
    min_quota: int = DEFAULT_MIN_QUOTA,
    max_pages: int | None = None,
    ttl_s: int = DEFAULT_TTL_S,
    refresh: bool = False,
    cache: CacheStats | None = None,
    default_branch: str | None = None,
) -> Iterator[PageResult]:
    """一页一页地挖，每页 yield 一次。**状态在 `state` 上就地更新。**

    做成生成器而不是"挖完再返回"：调用方要在每页之后落库和落盘，
    一次挖几千个 PR 的作业，中途被 Ctrl-C 打断也只丢最后一页。
    """
    pages_done = 0
    while True:
        pending = state.pending()
        if not pending:
            state.stopped_reason = "完成"
            return
        if max_pages is not None and pages_done >= max_pages:
            state.stopped_reason = "达到页数上限"
            return

        window = pending[0]
        variables: dict[str, Any] = {
            "q": search_query(state.repo, window.start, window.stop),
            "count": state.page_size,
        }
        if window.cursor:
            variables["cursor"] = window.cursor

        data, budget, hit = cached_graphql(
            PR_PAGE_QUERY,
            variables,
            timeout_s=PAGE_QUERY_TIMEOUT_S,
            ttl_s=ttl_s,
            # 重取降级页时必须绕过缓存：坏结果已经写进去了，
            # 不绕过的话后面几次重取全是同一份空 nodes
            refresh=refresh or window.degraded_attempts > 0,
            stats=cache,
        )
        pages_done += 1
        if budget:
            state.points_spent += budget.cost
            state.quota_remaining = budget.remaining
        reset_at = (data.get("rateLimit") or {}).get("resetAt")
        if reset_at:
            state.quota_reset_at = str(reset_at)

        search = data.get("search") or {}

        # 第一次看到这个窗口：先问它宽不宽。
        # GitHub 搜索单个查询最多给 1000 条，超了就必须二分，
        # 否则后面的 PR 会被静默丢掉 —— 不报错，只是产出率偏低。
        if window.issue_count is None:
            window.issue_count = int(search.get("issueCount") or 0)
            if window.issue_count >= SEARCH_RESULT_CAP:
                halves = bisect_window(
                    date.fromisoformat(window.start), date.fromisoformat(window.stop)
                )
                if halves is not None:
                    state.replace_window(
                        window, [(a.isoformat(), b.isoformat()) for a, b in halves]
                    )
                    # 这一页的结果丢掉：它属于旧窗口，两个子窗口会重新取。
                    # 代价是白发一次请求（1 个点），换来的是不漏 PR。
                    continue
                state.problems.append(
                    f"窗口 {window.start} 一天之内就有 {window.issue_count} 条，"
                    f"超过搜索上限 {SEARCH_RESULT_CAP}，只能取到前 {SEARCH_RESULT_CAP} 条"
                )

        nodes = search.get("nodes") or []

        # GitHub 偶尔返回一页"降级响应"：`issueCount` 说有 14 条，`nodes` 却是空的。
        # 退出码 0、没有 `errors` 字段，`github.graphql()` 那一层看不出任何异常
        # （2026-09-09 实测，挖 pallets/click 第一个窗口就撞上）。
        #
        # 照收的话这一页会被当成"窗口挖完了"，14 个 PR 一条不剩地消失，
        # 而报表上只表现为产出率低了一点 —— **静默丢数据是这里最贵的一种错**。
        #
        # 判据是"这一页什么都没有，而这个窗口还没走完"，不是光看 nodes 空不空：
        # 多页窗口的最后一页本来就可能是空的。
        if not nodes and window.short:
            window.degraded_attempts += 1
            if window.degraded_attempts <= DEGRADED_RETRIES:
                time.sleep(DEGRADED_BACKOFF_S)
                continue  # 不推进游标，下一轮带 refresh 重取这一页
            state.problems.append(
                f"窗口 {window.start}..{window.stop} 报了 {window.issue_count} 条，"
                f"但连取 {DEGRADED_RETRIES + 1} 次都返回空，只走到 {window.prs_seen} 条"
            )
            window.done = True
            window.cursor = None
            yield PageResult(window=window, candidates=(), from_cache=hit)
            continue
        window.degraded_attempts = 0

        candidates: list[MinedPR] = []
        for node in nodes:
            pr = parse_pr_node(node)
            if pr is None:
                continue  # search 返回里混进来的非 PR 节点是空对象
            window.prs_seen += 1
            reason = classify(pr)
            if reason is not None:
                state.count_reject(reason)
                continue
            if pr.suspect_base_commit:
                state.suspect_base_commit += 1
            if default_branch and pr.base_ref_name and pr.base_ref_name != default_branch:
                state.non_default_base_ref += 1
            if pr.files_truncated:
                state.files_truncated_ok += 1
            candidates.append(pr)

        window.candidates += len(candidates)
        window.pages += 1

        page_info = search.get("pageInfo") or {}
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            window.cursor = str(page_info["endCursor"])
        else:
            window.done = True
            window.cursor = None
            # 收尾自查：GitHub 说有多少条，我们就该走到多少条。
            # 对不上说明中间丢了页，而丢页同样不报错。
            if window.short:
                state.problems.append(
                    f"窗口 {window.start}..{window.stop} 报了 {window.issue_count} 条，"
                    f"实际只走到 {window.prs_seen} 条"
                )

        yield PageResult(window=window, candidates=tuple(candidates), from_cache=hit)

        # 配额判断放在 yield 之后：这一页的候选先落库，再决定要不要收工。
        if budget and budget.remaining < min_quota:
            state.stopped_reason = "配额不足"
            return


# ══════════════════════════════════════════════════════════════
# 落库
# ══════════════════════════════════════════════════════════════


def upsert_repository(
    session: Session, full_name: str, meta: dict[str, Any], *, is_domestic: bool
) -> Repository:
    """仓库行。`task_candidates.repository_id` 是外键，挖之前必须有这一行。

    已经存在就补齐会变的那几个字段（star、许可证、默认分支），
    不动 `mirror_path` —— 那是 E2-T1 物化工作区时写的，挖掘不该碰。
    """
    repo = session.execute(
        sa.select(Repository).where(Repository.full_name == full_name)
    ).scalar_one_or_none()
    license_id = ((meta.get("license") or {}) or {}).get("spdx_id")
    values = {
        "url": str(meta.get("html_url") or f"https://github.com/{full_name}"),
        "default_branch": str(meta.get("default_branch") or "main"),
        "language": str(meta.get("language") or "Python"),
        "stars": int(meta.get("stargazers_count") or 0),
        "license": license_id,
        "is_domestic": is_domestic,
    }
    if repo is None:
        repo = Repository(full_name=full_name, **values)
        session.add(repo)
        session.flush()
        return repo
    for key, value in values.items():
        setattr(repo, key, value)
    session.flush()
    return repo


@dataclass(frozen=True, slots=True)
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    #: 已经被 E1-T5 处理过（不再是 DISCOVERED），这次原样不动。
    left_alone: int = 0

    def __add__(self, other: UpsertCounts) -> UpsertCounts:
        return UpsertCounts(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.left_alone + other.left_alone,
        )


def upsert_candidates(
    session: Session, repository_id: int, prs: Sequence[MinedPR], *, repo: str
) -> UpsertCounts:
    """写 `task_candidates`。**"重复运行不产生重复候选"这条 AC 就靠这里。**

    幂等靠 `UNIQUE(repository_id, pr_number)`，不靠"我记得挖过了"。

    冲突时**只更新还是 `DISCOVERED` 的行**。已经被 E1-T5 打过分的
    （`PRESCREENED` / `REJECTED` / `PROMOTED`）原样不动 —— 重挖一遍就把
    人工核对过的预筛结果冲掉，那比重复候选严重得多。
    """
    if not prs:
        return UpsertCounts()

    numbers = [pr.number for pr in prs]
    existing: dict[int, TaskCandidateState] = {
        int(number): state
        for number, state in session.execute(
            sa.select(TaskCandidate.pr_number, TaskCandidate.state).where(
                TaskCandidate.repository_id == repository_id,
                TaskCandidate.pr_number.in_(numbers),
            )
        ).all()
    }
    inserted = len([n for n in numbers if n not in existing])
    updated = len([n for n in numbers if existing.get(n) is TaskCandidateState.DISCOVERED])
    left_alone = len(numbers) - inserted - updated

    rows = [
        {
            "repository_id": repository_id,
            "pr_number": pr.number,
            "issue_number": pr.issue_number,
            "raw_payload": build_payload(pr, repo=repo),
            "state": TaskCandidateState.DISCOVERED,
        }
        for pr in prs
    ]
    stmt = pg_insert(TaskCandidate).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_task_candidates_repo_pr",
            set_={
                "issue_number": stmt.excluded.issue_number,
                "raw_payload": stmt.excluded.raw_payload,
            },
            where=TaskCandidate.state == TaskCandidateState.DISCOVERED,
        )
    )
    return UpsertCounts(inserted=inserted, updated=updated, left_alone=left_alone)


# ══════════════════════════════════════════════════════════════
# E8-T1 的估计值，用来对照
# ══════════════════════════════════════════════════════════════


def survey_estimates() -> dict[str, int]:
    """读 E8-T1 的选型存档，取每个仓库估的候选池深度。

    对照它是这次挖掘顺带产生的一个结论：E8-T1 的候选池是**抽样估**出来的
    （关联 issue 的 PR 数 × 抽样里合格的比例），这里是**真挖**。
    两个数差多少，就是那套抽样方法的误差。

    候选池深度**当场从 `RepoFacts` 重算**，不读存档里那个同名的键：
    2026-09-08 那份存档是在派生量落盘之前写的，里面 `estimated_candidates`
    全是 `null`，直接读会让对照列整列变成"—"。重算只要
    `linked_prs_2y × (prs_candidate / prs_examined)`，输入都在存档里。

    key 统一转小写：选型名单里写的是 `hiyouga/LlamaFactory`，
    而 GitHub 上的规范写法是 `hiyouga/LLaMA-Factory`，大小写不一样。
    """
    archive = latest_archive()
    if archive is None or not archive.exists():
        return {}
    payload = json.loads(archive.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for row in payload.get("repos", []):
        estimate = RepoFacts.from_json(row).estimated_candidates
        if estimate is not None:
            out[str(row["full_name"]).lower()] = int(estimate)
    return out


def repo_group(full_name: str) -> str:
    """从 E8-T1 的候选名单里取这个仓库的分组（`国产/中文社区` 之类）。"""
    try:
        for name, group in load_candidates():
            if name.lower() == full_name.lower():
                return group
    except SystemExit:
        return ""
    return ""


# ══════════════════════════════════════════════════════════════
# run
# ══════════════════════════════════════════════════════════════


def cmd_run(args: argparse.Namespace) -> int:
    if not gh_available():
        print("本机没装 gh（GitHub CLI），挖不了。装好之后 `gh auth login`", file=sys.stderr)
        return 1

    until = date.fromisoformat(args.until) if args.until else datetime.now(UTC).date()
    since = date.fromisoformat(args.since) if args.since else default_since(until, args.window_days)
    if since > until:
        print(f"--since({since}) 比 --until({until}) 还晚", file=sys.stderr)
        return 1

    ttl_s = -1 if args.no_cache else args.cache_ttl
    engine = None
    session_factory = None
    if not args.dry_run:
        engine = create_db_engine()
        session_factory = create_session_factory(engine)

    states: list[MiningState] = []
    failures: list[str] = []
    for full_name in args.repo:
        # flush：stderr 不带缓冲、stdout 重定向到文件时带缓冲，不刷的话
        # 出错信息会跑到表头前面去，日志读起来像是"还没开始就失败了"
        print(
            f"\n{'═' * 66}\n挖 {full_name}  {since} .. {until}\n{'═' * 66}",
            flush=True,
        )
        try:
            state = _run_one(
                full_name,
                since=since,
                until=until,
                args=args,
                ttl_s=ttl_s,
                session_factory=session_factory,
            )
        except GitHubNotFoundError as exc:
            failures.append(f"{full_name}：仓库不存在或没有权限（{exc}）")
            print(f"  ✗ {failures[-1]}", file=sys.stderr)
            continue
        except GitHubError as exc:
            # 一个仓库失败不影响别的：它自己的进度已经落盘，下次接着挖
            failures.append(f"{full_name}：{exc}")
            print(f"  ✗ GitHub 那边出问题了：{exc}", file=sys.stderr)
            print("    进度已落盘，重跑这条命令会接着挖", file=sys.stderr)
            continue
        states.append(state)

    if engine is not None:
        engine.dispose()

    if states:
        print_report(states)
    files, total_bytes = cache_size()
    print(f"\n缓存：{files} 个文件，{total_bytes / 1024 / 1024:.1f} MB（{short(Path.cwd())} 无关）")
    for line in failures:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if failures and not states else 0


def _run_one(
    full_name: str,
    *,
    since: date,
    until: date,
    args: argparse.Namespace,
    ttl_s: int,
    session_factory: Any,
) -> MiningState:
    """挖一个仓库。落库和落盘都在这里，`mine_repo` 只管翻页。"""
    cache = CacheStats()
    meta = cached_rest(f"repos/{full_name}", ttl_s=ttl_s, refresh=args.refresh, stats=cache)
    default_branch = str(meta.get("default_branch") or "")

    is_domestic = args.domestic if args.domestic is not None else ("国产" in repo_group(full_name))

    state = resume_or_new(
        full_name,
        since=since,
        until=until,
        buckets=args.buckets,
        page_size=args.page_size,
        restart=args.restart,
    )
    if state.windows_done:
        print(
            f"  续跑：{len(state.windows)} 个窗口已完成 {state.windows_done} 个，"
            f"已有候选 {state.candidates} 条"
        )

    repository_id: int | None = None
    if session_factory is not None:
        with session_scope(session_factory) as session:
            repository_id = upsert_repository(session, full_name, meta, is_domestic=is_domestic).id

    counts = UpsertCounts()
    try:
        for page in mine_repo(
            state,
            min_quota=args.min_quota,
            max_pages=args.max_pages,
            ttl_s=ttl_s,
            refresh=args.refresh,
            cache=cache,
            default_branch=default_branch,
        ):
            if session_factory is not None and repository_id is not None and page.candidates:
                with session_scope(session_factory) as session:
                    counts += upsert_candidates(
                        session, repository_id, page.candidates, repo=full_name
                    )
            save_state(state)
            mark = "缓存" if page.from_cache else "联网"
            print(
                f"  [{mark}] {page.window.start}..{page.window.stop} "
                f"第 {page.window.pages} 页：扫 {page.window.prs_seen} 个 PR，"
                f"候选 {page.window.candidates} 条"
                f"（累计 {state.candidates}/{state.prs_seen}）",
                flush=True,
            )
    except KeyboardInterrupt:
        state.stopped_reason = "被中断"
        save_state(state)
        print("\n  已中断，进度落盘。重跑这条命令会接着挖", file=sys.stderr)
        raise
    except GitHubError:
        state.stopped_reason = "GitHub 出错"
        save_state(state)
        raise

    save_state(state)
    archive = write_report(state, meta=meta, is_domestic=is_domestic, cache=cache)
    print(f"\n  {state.stopped_reason}：候选 {state.candidates} 条 / 扫过 {state.prs_seen} 个 PR")
    if session_factory is not None:
        print(
            f"  落库：新增 {counts.inserted}，更新 {counts.updated}，"
            f"已被 E1-T5 处理过、原样不动 {counts.left_alone}"
        )
    else:
        print("  --dry-run：没有写库")
    print(f"  缓存命中 {cache.hits}/{cache.total}；本次烧掉 {state.points_spent} 个 GraphQL 点")
    print(f"  报表：{short(archive)}")
    return state


def write_report(
    state: MiningState, *, meta: dict[str, Any], is_domestic: bool, cache: CacheStats | None = None
) -> Path:
    """把这次挖掘的产出率写进 `datasets/mining/`，KB 级、进版本库。

    **必须连缓存命中一起记。** `points_spent: 0` 有两种完全相反的含义 ——
    "全命中缓存所以免费"和"根本没干活"。只记点数的话文件里分不出来，
    而这个文件是要进版本库、几个月后给人当依据的。

    2026-09-09 真踩到：一次纯缓存重建把"26 个点"覆盖成了 0，
    和 §8.9 里写的数字直接对不上。预筛那个存档没这毛病，
    因为它记的是 `{calls, cached_calls, billed_calls}` 三个数。
    """
    path = report_path(state.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo": state.repo,
        "cache": (cache or CacheStats()).to_json(),
        #: 这一轮有没有真发过网络请求。全命中缓存时 `points_spent` 是 0，
        #: 那个 0 不代表"挖掘不花配额"。
        "served_from_cache": bool(cache and cache.misses == 0 and cache.hits > 0),
        "group": repo_group(state.repo),
        "is_domestic": is_domestic,
        "default_branch": meta.get("default_branch"),
        "stars": meta.get("stargazers_count"),
        "license": ((meta.get("license") or {}) or {}).get("spdx_id"),
        "survey_estimate": survey_estimates().get(state.repo.lower()),
        "state": state.to_json(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════
# 报表
# ══════════════════════════════════════════════════════════════


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def print_report(states: Sequence[MiningState]) -> None:
    """候选产出率报表（AC 里点名要的那张表）。"""
    estimates = survey_estimates()
    print()
    header = (
        f"{'仓库':30} {'扫过 PR':>8} {'候选':>6} {'产出率':>7} "
        f"{'页':>4} {'点':>5} {'窗口':>7} {'E8-T1 估':>9}"
    )
    print(header)
    print("-" * len(header))
    for s in states:
        estimate = estimates.get(s.repo.lower())
        print(
            f"{s.repo:30} {s.prs_seen:>8} {s.candidates:>6} {_pct(s.yield_rate):>7} "
            f"{s.pages:>4} {s.points_spent:>5} "
            f"{f'{s.windows_done}/{len(s.windows)}':>7} "
            f"{('—' if estimate is None else str(estimate)):>9}"
        )

    print("\n漏斗（扫过的 PR 都去哪了）：")
    for s in states:
        parts = "  ".join(f"{label} {count}" for label, count in funnel(s))
        print(f"  {s.repo}：{parts}")

    print("\n值得留意的：")
    for s in states:
        notes = []
        if s.suspect_base_commit:
            notes.append(
                f"{s.suspect_base_commit} 条的 base_commit 可疑"
                "（单个父提交 + 多个提交，可能是 rebase merge）"
            )
        if s.non_default_base_ref:
            notes.append(f"{s.non_default_base_ref} 条合并进的不是默认分支")
        if s.files_truncated_ok:
            notes.append(f"{s.files_truncated_ok} 条改了 100 个以上的文件，路径清单不全")
        if s.stopped_reason and s.stopped_reason != "完成":
            notes.append(f"停在「{s.stopped_reason}」，还剩 {len(s.pending())} 个窗口没挖")
        for problem in s.problems:
            notes.append(problem)
        if notes:
            print(f"  {s.repo}：" + "；".join(notes))
        else:
            print(f"  {s.repo}：无")

    total = summarize(states)
    spent = total["points_spent"]
    # 0 点有两种相反的含义，必须说清是哪一种
    cost = (
        f"烧掉 {spent} 个 GraphQL 点（上限 5000 点/小时）"
        if spent
        else "本轮全部命中缓存，没发一次网络请求（**不代表挖掘不花配额**）"
    )
    print(
        f"\n合计：{total['repos']} 个仓库，扫过 {total['prs_seen']} 个 PR，"
        f"产出候选 {total['candidates']} 条，产出率 {_pct(total['yield_rate'])}，{cost}"
    )
    print(
        "\n  「候选」= 关联了 issue + 改了测试 + 改了源码 的 merged PR（§8.4 的三条自动过滤）。"
        "\n  脱敏、抽 F2P、LLM 预筛是 E1-T5 的活，这一步只把原始数据抄下来。"
    )


def cmd_report(args: argparse.Namespace) -> int:
    """从存档里出报表。不联网、不查库。"""
    if args.file:
        paths = [Path(f) for f in args.file]
    else:
        paths = sorted(REPORT_DIR.glob("*.json"))
        if args.repo:
            wanted = {r.replace("/", "__").lower() for r in args.repo}
            paths = [p for p in paths if p.stem.rsplit("-", 3)[0].lower() in wanted]
    if not paths:
        print(f"{short(REPORT_DIR)} 下没有存档，先跑 `python -m cli.mine run`", file=sys.stderr)
        return 1

    # 同一个仓库有多份存档时只取最新那份（文件名带日期，字典序就是时间序）
    latest: dict[str, Path] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        latest[str(payload["repo"])] = path
    states = [
        MiningState.from_json(json.loads(p.read_text(encoding="utf-8"))["state"])
        for p in latest.values()
    ]
    for path in latest.values():
        print(f"数据来自 {short(path)}")
    print_report(sorted(states, key=lambda s: -s.candidates))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """库里现在有多少候选，按仓库和状态分组。"""
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            rows = session.execute(
                sa.select(
                    Repository.full_name,
                    TaskCandidate.state,
                    sa.func.count(TaskCandidate.id),
                )
                .join(Repository, Repository.id == TaskCandidate.repository_id)
                .group_by(Repository.full_name, TaskCandidate.state)
                .order_by(Repository.full_name)
            ).all()
    finally:
        engine.dispose()

    if not rows:
        print("task_candidates 是空的。先跑 `python -m cli.mine run --repo <owner/name>`")
        return 0

    by_repo: dict[str, dict[str, int]] = {}
    for full_name, state, count in rows:
        by_repo.setdefault(full_name, {})[str(state)] = count
    states = [s.value for s in TaskCandidateState]
    header = f"{'仓库':30} " + " ".join(f"{s:>12}" for s in states) + f" {'合计':>7}"
    print(header)
    print("-" * len(header))
    for full_name, counts in sorted(by_repo.items()):
        cells = " ".join(f"{counts.get(s, 0):>12}" for s in states)
        print(f"{full_name:30} {cells} {sum(counts.values()):>7}")
    print(f"\n合计 {sum(sum(c.values()) for c in by_repo.values())} 条候选")
    print("  DISCOVERED = E1-T4 挖出来、还没预筛；PRESCREENED 之后是 E1-T5 的事")
    return 0


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.mine", description="GitHub 挖掘器（E1-T4，§8.4）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="挖一个或多个仓库，候选写进 task_candidates")
    p_run.add_argument("--repo", action="append", required=True, help="owner/name，可以给多个")
    p_run.add_argument("--since", help="只挖这个日期之后合并的 PR，默认近两年")
    p_run.add_argument("--until", help="默认今天")
    p_run.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="--since 不给时往回挖几天"
    )
    p_run.add_argument(
        "--buckets", type=int, default=DEFAULT_BUCKETS, help="把时间范围切成几个窗口"
    )
    p_run.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="一页取几个 PR")
    p_run.add_argument("--max-pages", type=int, help="最多翻几页（调试用）")
    p_run.add_argument(
        "--min-quota", type=int, default=DEFAULT_MIN_QUOTA, help="GraphQL 配额低于它就停下来落盘"
    )
    p_run.add_argument("--cache-ttl", type=int, default=DEFAULT_TTL_S, help="缓存过期秒数")
    p_run.add_argument("--no-cache", action="store_true", help="完全不用缓存")
    p_run.add_argument("--refresh", action="store_true", help="忽略已有缓存，重新拉一遍")
    p_run.add_argument("--restart", action="store_true", help="丢掉进度从头挖")
    p_run.add_argument("--dry-run", action="store_true", help="不写库，只看产出率")
    p_run.add_argument(
        "--domestic",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是不是国产项目，默认按 E8-T1 的候选名单分组判",
    )
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="候选产出率报表（读存档，不联网）")
    p_report.add_argument("--repo", action="append", help="只看这几个仓库")
    p_report.add_argument("--file", action="append", help="直接指定存档文件")
    p_report.set_defaults(func=cmd_report)

    p_show = sub.add_parser("show", help="库里现在有多少候选")
    p_show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REPORT_DIR",
    "STATE_DIR",
    "PageResult",
    "UpsertCounts",
    "build_parser",
    "load_state",
    "main",
    "mine_repo",
    "resume_or_new",
    "save_state",
    "state_path",
    "upsert_candidates",
    "upsert_repository",
]
