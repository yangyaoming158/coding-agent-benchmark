"""GitHub 挖掘器的判断逻辑（E1-T4，`03-benchmark-spec.md` §8.4）。

一句话：**把"某个仓库近两年哪些 merged PR 能派生出题目"这件事变成纯函数。**

联网的部分（打 GraphQL、落库、写进度文件）在 `cli/mine.py`。这里只有三件事：

1. **时间窗怎么切**（`split_window` / `bisect_window`）
2. **GraphQL 返回的一个节点怎么变成一条候选**（`parse_pr_node` / `classify`）
3. **挖到哪儿了、产出率多少**（`MiningState`）

分开是为了能不联网测：真正难的判断全在这一层，而它一次网络请求都不用发。

## 这一步只抄，不改

E1-T4 的产出是 `task_candidates` 里 `state=DISCOVERED` 的行，`raw_payload`
存 GitHub 原样返回的东西。**脱敏、拆 test_patch/code_patch、抽候选 F2P、
LLM 预筛全部是 E1-T5 的活**（§8.4 那十步的后五步）。

这条边界不是洁癖：挖掘失败是网络和配额问题，重跑就好；脱敏失败是**泄题**，
漏一条 PR 链接那道题就废了，要靠正则断言加人工抽查兜（E1-T5 的 AC）。
混在一起的话，一次限流重跑会把已经核对过的脱敏结果连带重做一遍。

所以这里也**不取 PR 的 diff 正文**：拆补丁是 E1-T5 的活，而一个 PR 的 diff
动辄几十上百 KB，塞进 `raw_payload` 这个 JSONB 列会让一张几千行的表很难查。
这里只存文件路径清单 —— 过滤用得着，也够 E1-T5 判断要不要去取 diff。

## 为什么必须按时间窗切，不能从最新的一路往下翻

两个各自独立的理由：

1. **抽样会被带偏**（§8.8 的 nonebot2 教训）：它最近几百个关联 issue 的 merged PR
   几乎全是插件商店的 registry 条目，只改一个 `plugins.json5`。
2. **GitHub 搜索单个查询最多返回 1000 条。** sqlfluff 近 2 年关联 issue 的
   merged PR 就超了，不切窗口后面的直接拿不到。

所以默认把 `--since` 到今天切成 12 段（约每 2 个月一段），段内用游标翻页；
某一段的 `issueCount` 仍然 ≥1000 就把它二分再切（`bisect_window`）。

## 关联口径只认 GitHub 的 `linked:issue`

§8.8 实测过放宽口径（正文里出现 `#123` 也算关联）：pyecharts 1 → 6，
nonebot2 反而更低（8 → 4），akshare 宽口径抽 150 个**一个合格的都没有**。
瓶颈是"带测试的 bugfix PR"本身稀少，不是关联方式。所以这里只走严口径。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.benchmark.survey import touches_source_code, touches_test_code
from app.domain.protected_paths import TEST_CODE_PATTERNS, is_protected

#: `raw_payload` 的形状版本。E1-T5 读到不认识的版本要能立刻发现，
#: 而不是默默按旧字段名取到 `None`。
MINER_VERSION = "1.0"

#: 挖多久以前的 PR。和 §8.3 的候选池口径（近 2 年）保持一致 ——
#: 两个数不一样的话，E8-T1 估的候选池和这里真挖出来的就不可比了。
DEFAULT_WINDOW_DAYS = 730

#: 默认把整段时间切成几个窗口。12 段 ≈ 每 2 个月一段。
DEFAULT_BUCKETS = 12

#: 一页取几个 PR。**实测每页只花 1 个 GraphQL 点**（2026-09-09，25 个 PR
#: 各展开 100 个文件，`rateLimit.cost` 回报 1），所以页大小不是配额问题，
#: 是**超时**问题：E8-T1 探 PaddleOCR 时一页 25 个就让 GitHub 算了一分多钟。
#: 25 是实测跑得稳的值。
DEFAULT_PAGE_SIZE = 25

#: 一个 PR 最多展开多少个文件。GitHub 的 `files` 连接上限就是 100。
FILES_PER_PR = 100

#: 一个 PR 最多取几个关联 issue。绝大多数只有 1 个；取 5 个是为了让
#: "这个 PR 关联了好几个 issue"在数据里看得见，那种 PR 的题面往往不自足。
ISSUES_PER_PR = 5

#: GitHub 搜索单个查询的返回上限。到这个数就说明窗口太宽，要二分。
SEARCH_RESULT_CAP = 1000

#: 配额低于这个数就停下来落盘。留余量是因为停下来之前还要把当前页处理完。
DEFAULT_MIN_QUOTA = 500

#: 展开文件列表那个查询的超时。默认 120 秒对大仓库不够（§8.8 实测）。
PAGE_QUERY_TIMEOUT_S = 240

#: 一页"降级响应"最多重取几次，以及每次之前等多久（秒）。
#:
#: **降级响应**指的是：GitHub 说这个窗口有 14 条，`nodes` 却是空的 ——
#: 退出码 0、没有 `errors` 字段，看起来完全正常（2026-09-09 实测，
#: 挖 pallets/click 第一个窗口就撞上，紧跟在一次 "Something went wrong" 重试之后）。
#:
#: 照收的话就是**静默丢数据**：那一页会被当成"这个窗口挖完了"，14 个 PR
#: 一条不剩地消失，而报表上只表现为产出率低了一点。所以宁可多花几次请求。
DEGRADED_RETRIES = 3
DEGRADED_BACKOFF_S = 5.0


# ══════════════════════════════════════════════════════════════
# GraphQL 查询
# ══════════════════════════════════════════════════════════════

#: 一页 PR，连文件列表和关联 issue 一起取回来。
#:
#: **issue 正文取 `body` 不是 `bodyText`**：`bodyText` 会把 markdown 拍平，
#: 围栏代码块的 ``` 标记没了。而判语言（`survey.detect_language`）和
#: E1-T5 的脱敏都要先剥代码块 —— 拍平之后剥不掉，一个贴了 200 行英文日志的
#: 中文 issue 就会被判成英文。
#:
#: `mergeCommit.parents` 连**每个父提交属于哪个 PR** 一起取（`associatedPullRequests`）。
#: 多这一层是为了把 rebase merge 认出来 —— 只看"有几个父提交"会把 squash merge
#: 一起冤枉进去，而 squash 的 `parents[0]` 是对的。判据见 `MinedPR.suspect_base_commit`。
PR_PAGE_QUERY = f"""
query($q: String!, $count: Int!, $cursor: String) {{
  rateLimit {{ cost remaining limit resetAt }}
  search(query: $q, type: ISSUE, first: $count, after: $cursor) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on PullRequest {{
        number
        title
        url
        createdAt
        mergedAt
        baseRefName
        baseRefOid
        additions
        deletions
        changedFiles
        commits {{ totalCount }}
        mergeCommit {{
          oid
          parents(first: 5) {{
            totalCount
            nodes {{ oid associatedPullRequests(first: 2) {{ nodes {{ number }} }} }}
          }}
        }}
        author {{ login }}
        files(first: {FILES_PER_PR}) {{
          totalCount
          nodes {{ path additions deletions changeType }}
        }}
        closingIssuesReferences(first: {ISSUES_PER_PR}) {{
          totalCount
          nodes {{ number title body url createdAt state }}
        }}
      }}
    }}
  }}
}}
"""


def search_query(repo: str, start: str, stop: str) -> str:
    """拼一条搜索表达式。`merged:A..B` 两端都是闭区间，所以窗口之间不能有重叠日。"""
    return f"repo:{repo} is:pr is:merged linked:issue merged:{start}..{stop}"


# ══════════════════════════════════════════════════════════════
# 时间窗
# ══════════════════════════════════════════════════════════════


def split_window(since: date, until: date, buckets: int) -> list[tuple[date, date]]:
    """把 `[since, until]` 均匀切成若干个**闭区间**，首尾相接不重叠。

    天数不够分时段数自动缩小 —— 切成零长度的窗口会白发一次请求。
    """
    total_days = (until - since).days + 1
    if total_days <= 0:
        return []
    count = max(1, min(buckets, total_days))
    edges = [since + timedelta(days=round(total_days * i / count)) for i in range(count + 1)]
    windows: list[tuple[date, date]] = []
    for i in range(count):
        start = edges[i]
        stop = edges[i + 1] - timedelta(days=1)
        if stop >= start:
            windows.append((start, stop))
    return windows


def bisect_window(start: date, stop: date) -> tuple[tuple[date, date], tuple[date, date]] | None:
    """把一个窗口二分。只剩一天时返回 `None` —— 再切不下去了。

    什么时候用：某个窗口的 `issueCount` 撞到 1000 条上限，说明它太宽，
    翻页只能拿到前 1000 条，剩下的会被静默丢掉。
    """
    if start >= stop:
        return None
    mid = start + timedelta(days=(stop - start).days // 2)
    return (start, mid), (mid + timedelta(days=1), stop)


def default_since(until: date, days: int = DEFAULT_WINDOW_DAYS) -> date:
    return until - timedelta(days=days - 1)


# ══════════════════════════════════════════════════════════════
# 一个 PR 节点 → 一条候选
# ══════════════════════════════════════════════════════════════

#: 丢弃理由。写成常量而不是散在代码里，因为报表的漏斗每一格就是它们。
REJECT_NO_LINKED_ISSUE = "NO_LINKED_ISSUE"
REJECT_NO_TEST_FILES = "NO_TEST_FILES"
REJECT_NO_SOURCE_FILES = "NO_SOURCE_FILES"
REJECT_FILES_TRUNCATED = "FILES_TRUNCATED"
REJECT_NO_BASE_COMMIT = "NO_BASE_COMMIT"

#: 丢弃理由的人话，报表直接用。
REJECT_LABELS: dict[str, str] = {
    REJECT_NO_LINKED_ISSUE: "没有关联 issue",
    REJECT_NO_TEST_FILES: "没改测试",
    REJECT_NO_SOURCE_FILES: "没改源码",
    REJECT_FILES_TRUNCATED: "文件超过 100 个，判不了",
    REJECT_NO_BASE_COMMIT: "取不到 base_commit",
}


@dataclass(frozen=True, slots=True)
class LinkedIssue:
    """PR 通过 `closes #N` 关联上的一个 issue。正文是**原始 markdown，未脱敏**。"""

    number: int
    title: str
    body: str
    url: str
    created_at: str | None = None
    state: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "created_at": self.created_at,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class MinedPR:
    """一个 merged PR 上我们关心的全部事实。字段名对着 §7.1 的任务 Schema 起。"""

    number: int
    title: str
    url: str
    merged_at: str | None
    created_at: str | None
    base_ref_name: str | None
    base_ref_oid: str | None
    merge_commit_oid: str | None
    #: merge commit 的全部父提交。`parents[0]` 就是 §8.4 说的 base_commit。
    merge_parents: tuple[str, ...]
    #: `parents[0]` 这个提交属于哪几个 PR。判 rebase merge 用，见 `suspect_base_commit`。
    first_parent_pull_requests: tuple[int, ...]
    commits_total: int
    additions: int
    deletions: int
    changed_files: int
    author: str | None
    #: 这个 PR 改动的文件路径（最多 100 条，见 `files_truncated`）。
    paths: tuple[str, ...]
    files_total: int
    issues: tuple[LinkedIssue, ...]
    linked_total: int

    @property
    def files_truncated(self) -> bool:
        """文件多到 GitHub 一页放不下。这时路径清单是不全的。"""
        return self.files_total > len(self.paths)

    @property
    def base_commit(self) -> str | None:
        """§8.4：取 `parents[0]`。"""
        return self.merge_parents[0] if self.merge_parents else None

    @property
    def suspect_base_commit(self) -> bool:
        """`base_commit` 可能不对 —— **rebase merge 的坑**。

        三种合并方式，`parents[0]` 的含义不一样：

        - **merge commit**（2 个父提交）：`parents[0]` 就是合并时的基线，**对**。
        - **squash merge**（1 个父提交）：squash 出来的提交直接接在基线上，**也对**。
        - **rebase merge**：合并后的"merge commit"是被 rebase 过的**最后一个**提交，
          它的父提交是倒数第二个 rebase 提交，**不是基线** —— 算出来的 base_commit
          偏后，工作区里会已经带上这个 PR 的一部分改动。题目看起来正常，
          只是被测 AI 拿到的代码已经修好一半了。

        **判据：`parents[0]` 这个提交是不是也属于本 PR。** 是就说明它是被 rebase
        进来的、不是基线；不是就说明它是基线上的前一个提交。

        为什么不用"父提交只有一个 + PR 有多个提交"：那会把 squash merge 一起冤枉
        进去，而 squash 的 `parents[0]` 是对的。2026-09-09 实测 pallets/click
        一个窗口 16 条候选，按那个判据 15 条"可疑"，按这一条 0 条 ——
        一个 94% 命中的告警等于没有告警。

        只报可疑、不丢弃。真正的兜底是 E1-T3 验证流水线的 S5：base_commit 不对的话
        F2P 在基线上不会失败，题目会被判 `F2P_NOT_FAILING` 丢掉，混不进数据集。
        """
        return self.number in self.first_parent_pull_requests

    @property
    def test_paths(self) -> tuple[str, ...]:
        """改动里属于测试用例的那些。用 `TEST_CODE_PATTERNS`，不是整份受保护清单
        （后者含 `pyproject.toml`、`.github/**`，会把只改配置的 PR 算成带了测试）。
        """
        return tuple(p for p in self.paths if is_protected(p, TEST_CODE_PATTERNS))

    @property
    def source_paths(self) -> tuple[str, ...]:
        """改动里**不受任何保护**的那些 —— 也就是被测 AI 真正要改的部分。"""
        return tuple(p for p in self.paths if not is_protected(p))

    @property
    def issue_number(self) -> int | None:
        """落库用哪个 issue 号。

        多个关联 issue 时取**号最小**的那个：先被提的通常才是那份 bug 报告，
        后面的往往是"顺手也修了"的引用。全部关联 issue 都存进 `raw_payload`，
        真要挑还是 E1-T5 的事，这里只要一个确定的值。
        """
        return min((i.number for i in self.issues), default=None)


def parse_pr_node(node: dict[str, Any] | None) -> MinedPR | None:
    """GraphQL 的一个搜索结果节点 → `MinedPR`。不是 PR 或者空对象返回 `None`。

    每个可空字段都兜一次底：GraphQL 里 `author`（用户注销了）、`mergeCommit`、
    `files`（PR 太大 GitHub 拒绝展开）都可能是 `null`。漏判一个会在翻到第几百页时
    崩掉，而那时前面的进度还没落盘。
    """
    if not node or "number" not in node:
        return None

    files = (node.get("files") or {}).get("nodes") or []
    paths = tuple(str(f["path"]) for f in files if f and f.get("path"))

    merge_commit = node.get("mergeCommit") or {}
    parent_nodes = [p for p in ((merge_commit.get("parents") or {}).get("nodes") or []) if p]
    parents = tuple(str(p["oid"]) for p in parent_nodes if p.get("oid"))
    first_parent_prs = (
        tuple(
            int(a["number"])
            for a in ((parent_nodes[0].get("associatedPullRequests") or {}).get("nodes") or [])
            if a and a.get("number") is not None
        )
        if parent_nodes
        else ()
    )

    refs = node.get("closingIssuesReferences") or {}
    issues = tuple(
        LinkedIssue(
            number=int(n["number"]),
            title=str(n.get("title") or ""),
            body=str(n.get("body") or ""),
            url=str(n.get("url") or ""),
            created_at=n.get("createdAt"),
            state=n.get("state"),
        )
        for n in (refs.get("nodes") or [])
        if n and n.get("number") is not None
    )

    return MinedPR(
        number=int(node["number"]),
        title=str(node.get("title") or ""),
        url=str(node.get("url") or ""),
        merged_at=node.get("mergedAt"),
        created_at=node.get("createdAt"),
        base_ref_name=node.get("baseRefName"),
        base_ref_oid=node.get("baseRefOid"),
        merge_commit_oid=merge_commit.get("oid"),
        merge_parents=parents,
        first_parent_pull_requests=first_parent_prs,
        commits_total=int((node.get("commits") or {}).get("totalCount") or 0),
        additions=int(node.get("additions") or 0),
        deletions=int(node.get("deletions") or 0),
        changed_files=int(node.get("changedFiles") or 0),
        author=(node.get("author") or {}).get("login"),
        paths=paths,
        files_total=int((node.get("files") or {}).get("totalCount") or len(paths)),
        issues=issues,
        linked_total=int(refs.get("totalCount") or len(issues)),
    )


def classify(pr: MinedPR) -> str | None:
    """这个 PR 能不能派生出一道题。能就返回 `None`，不能就返回丢弃理由。

    §8.4 的三条自动过滤（无关联 issue / 无测试 / 无源码），加两条实现上必须有的：

    - **文件列表被截断且判不出结论**：PR 改了 100 个以上的文件，我们只看得到前 100 个。
      前 100 个里既有测试又有源码就照常收（结论已经成立，只是清单不全）；
      否则丢弃 —— 当成候选是在瞎猜，当成"没改测试"又可能冤枉它。
    - **取不到 base_commit**：没有 merge commit 或者它没有父提交，题目立不起来。

    检查顺序就是报表漏斗的顺序，别随手调。
    """
    if pr.linked_total <= 0 or not pr.issues:
        return REJECT_NO_LINKED_ISSUE

    has_test = touches_test_code(pr.paths)
    has_source = touches_source_code(pr.paths)
    if pr.files_truncated and not (has_test and has_source):
        return REJECT_FILES_TRUNCATED
    if not has_test:
        return REJECT_NO_TEST_FILES
    if not has_source:
        return REJECT_NO_SOURCE_FILES
    if not pr.base_commit:
        return REJECT_NO_BASE_COMMIT
    return None


def build_payload(pr: MinedPR, *, repo: str) -> dict[str, Any]:
    """`task_candidates.raw_payload` 存什么。

    原则：**GitHub 说什么就存什么，一个字不改。** 脱敏是 E1-T5 的事，
    在这里顺手做了的话，E1-T5 就再也看不到原文、没法核对自己脱干净了没有。

    不存 diff 正文（理由见模块文档）。存的是够 E1-T5 干活的元数据：
    路径清单、base_commit 的几种来源、issue 原文。
    """
    return {
        "miner_version": MINER_VERSION,
        "mined_at": datetime.now(UTC).isoformat(),
        "repo": repo,
        "pr": {
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "created_at": pr.created_at,
            "merged_at": pr.merged_at,
            "author": pr.author,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files": pr.changed_files,
            "commits_total": pr.commits_total,
            "base_ref_name": pr.base_ref_name,
            "base_ref_oid": pr.base_ref_oid,
            "merge_commit_oid": pr.merge_commit_oid,
            "merge_parents": list(pr.merge_parents),
            "first_parent_pull_requests": list(pr.first_parent_pull_requests),
        },
        # §8.4 的"取 parents[0] → base_commit"。`suspect` 那一条见 MinedPR 的说明。
        "base_commit": pr.base_commit,
        "base_commit_suspect": pr.suspect_base_commit,
        "files": {
            "total": pr.files_total,
            "truncated": pr.files_truncated,
            "paths": list(pr.paths),
            "test_paths": list(pr.test_paths),
            "source_paths": list(pr.source_paths),
        },
        "issues": [i.to_json() for i in pr.issues],
        "linked_issue_total": pr.linked_total,
    }


# ══════════════════════════════════════════════════════════════
# 挖到哪儿了（断点续跑的状态）
# ══════════════════════════════════════════════════════════════


@dataclass
class WindowState:
    """一个时间窗的进度。`cursor` 是 GitHub 的翻页游标，续跑时从它接着走。"""

    start: str
    stop: str
    cursor: str | None = None
    issue_count: int | None = None
    pages: int = 0
    prs_seen: int = 0
    candidates: int = 0
    done: bool = False
    #: 这一页连着拿到几次"降级响应"（见 `DEGRADED_RETRIES`）。取到东西就清零。
    degraded_attempts: int = 0

    @property
    def short(self) -> bool:
        """GitHub 说有这么多条，我们实际只走到这么多条。

        窗口挖完时用它自查。对不上就说明中间丢了页，**而丢页不会报错** ——
        只表现为产出率莫名其妙低了一点。
        """
        return self.issue_count is not None and self.prs_seen < self.issue_count

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> WindowState:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class MiningState:
    """一个仓库挖掘作业的全部状态。**这就是断点续跑的落盘内容。**

    为什么状态放文件不放库：候选本身进库（`task_candidates`），那是评测数据；
    "翻到第几页了"是**作业**的私事，跟着 `var/` 走。而且 `make check` 会清库，
    进度跟着一起没了的话，每跑一次 check 就要重挖一遍。
    """

    repo: str
    since: str
    until: str
    page_size: int
    windows: list[WindowState] = field(default_factory=list)
    #: 丢弃理由 → 条数。报表的漏斗。
    rejects: dict[str, int] = field(default_factory=dict)
    #: `parents[0]` 可能不对的条数（疑似 rebase merge）。
    suspect_base_commit: int = 0
    #: 合并进的不是仓库默认分支的条数（比如 click 的 `stable` 维护分支）。
    non_default_base_ref: int = 0
    #: 文件列表被截断、但前 100 个里已经能判出结论的条数。
    files_truncated_ok: int = 0
    #: 这次作业一共烧掉多少 GraphQL 点。命中缓存的页不计。
    points_spent: int = 0
    quota_remaining: int | None = None
    quota_reset_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    #: 为什么停的：`完成` / `配额不足` / `达到页数上限` / `被中断`。
    stopped_reason: str | None = None
    #: 挖的过程中出的问题，人话，原样进报表。和 `RepoFacts.problems` 一个用法。
    problems: list[str] = field(default_factory=list)

    # ── 派生量 ──

    @property
    def prs_seen(self) -> int:
        return sum(w.prs_seen for w in self.windows)

    @property
    def candidates(self) -> int:
        return sum(w.candidates for w in self.windows)

    @property
    def pages(self) -> int:
        return sum(w.pages for w in self.windows)

    @property
    def windows_done(self) -> int:
        return sum(1 for w in self.windows if w.done)

    @property
    def finished(self) -> bool:
        return bool(self.windows) and all(w.done for w in self.windows)

    @property
    def yield_rate(self) -> float | None:
        """候选产出率 = 候选数 / 扫过的 PR 数。AC 里那张"候选产出率报表"的主指标。"""
        seen = self.prs_seen
        return self.candidates / seen if seen else None

    def pending(self) -> list[WindowState]:
        return [w for w in self.windows if not w.done]

    def count_reject(self, reason: str) -> None:
        self.rejects[reason] = self.rejects.get(reason, 0) + 1

    def replace_window(self, target: WindowState, halves: Sequence[tuple[str, str]]) -> None:
        """把一个太宽的窗口就地换成它的两半，顺序不变。

        就地替换而不是追加到末尾：窗口在列表里是按时间排的，
        追加会让"挖到哪儿了"这个进度看起来在时间上乱跳。
        """
        index = self.windows.index(target)
        self.windows[index : index + 1] = [WindowState(start=a, stop=b) for a, b in halves]

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "since": self.since,
            "until": self.until,
            "page_size": self.page_size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stopped_reason": self.stopped_reason,
            "windows": [w.to_json() for w in self.windows],
            "rejects": dict(self.rejects),
            "suspect_base_commit": self.suspect_base_commit,
            "non_default_base_ref": self.non_default_base_ref,
            "files_truncated_ok": self.files_truncated_ok,
            "points_spent": self.points_spent,
            "quota_remaining": self.quota_remaining,
            "quota_reset_at": self.quota_reset_at,
            "problems": list(self.problems),
            # 派生量也写进去：报表和事后分析直接读，不用各自再算一遍
            "prs_seen": self.prs_seen,
            "candidates": self.candidates,
            "pages": self.pages,
            "windows_done": self.windows_done,
            "windows_total": len(self.windows),
            "yield_rate": self.yield_rate,
            "finished": self.finished,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> MiningState:
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in payload.items() if k in known and k != "windows"}
        kwargs["windows"] = [WindowState.from_json(w) for w in payload.get("windows", [])]
        return cls(**kwargs)


def new_state(
    repo: str,
    *,
    since: date,
    until: date,
    buckets: int = DEFAULT_BUCKETS,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> MiningState:
    """开一份新的挖掘状态，时间窗按 `buckets` 切好。"""
    now = datetime.now(UTC).isoformat()
    return MiningState(
        repo=repo,
        since=since.isoformat(),
        until=until.isoformat(),
        page_size=page_size,
        windows=[
            WindowState(start=a.isoformat(), stop=b.isoformat())
            for a, b in split_window(since, until, buckets)
        ],
        created_at=now,
        updated_at=now,
    )


def funnel(state: MiningState) -> list[tuple[str, int]]:
    """报表用的漏斗：`[(人话, 条数)]`，从"扫过的 PR"一路到"候选"。"""
    rows = [("扫过的 PR", state.prs_seen)]
    rows += [
        (REJECT_LABELS.get(code, code), count)
        for code, count in sorted(state.rejects.items(), key=lambda kv: -kv[1])
    ]
    rows.append(("候选", state.candidates))
    return rows


def summarize(states: Iterable[MiningState]) -> dict[str, Any]:
    """多个仓库的汇总。CLI 的 `report` 子命令拿它打表。"""
    items = list(states)
    seen = sum(s.prs_seen for s in items)
    candidates = sum(s.candidates for s in items)
    rejects: dict[str, int] = {}
    for s in items:
        for code, count in s.rejects.items():
            rejects[code] = rejects.get(code, 0) + count
    return {
        "repos": len(items),
        "prs_seen": seen,
        "candidates": candidates,
        "yield_rate": candidates / seen if seen else None,
        "rejects": rejects,
        "points_spent": sum(s.points_spent for s in items),
        "pages": sum(s.pages for s in items),
    }


__all__ = [
    "DEFAULT_BUCKETS",
    "DEFAULT_MIN_QUOTA",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_WINDOW_DAYS",
    "DEGRADED_BACKOFF_S",
    "DEGRADED_RETRIES",
    "FILES_PER_PR",
    "MINER_VERSION",
    "PAGE_QUERY_TIMEOUT_S",
    "PR_PAGE_QUERY",
    "REJECT_FILES_TRUNCATED",
    "REJECT_LABELS",
    "REJECT_NO_BASE_COMMIT",
    "REJECT_NO_LINKED_ISSUE",
    "REJECT_NO_SOURCE_FILES",
    "REJECT_NO_TEST_FILES",
    "SEARCH_RESULT_CAP",
    "LinkedIssue",
    "MinedPR",
    "MiningState",
    "WindowState",
    "bisect_window",
    "build_payload",
    "classify",
    "default_since",
    "funnel",
    "new_state",
    "parse_pr_node",
    "search_query",
    "split_window",
    "summarize",
]
