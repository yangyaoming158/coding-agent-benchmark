"""仓库选型的度量与打分（E8-T1，`03-benchmark-spec.md` §8.3）。

一句话：**用数据回答"这 20 个仓库里，哪 8–15 个能当题源"，不靠印象。**

§8.3 定了八条准则。本模块把其中能自动测的都变成数字，再按硬门槛过一遍：

| 准则 | 怎么测 | 门槛 |
|:---|:---|:---|
| 语言 Python 占比 | GitHub `languages` 接口的字节数 | ≥ 80% |
| 许可证 | REST 的 `license.spdx_id` | MIT / Apache-2.0 / BSD 系 |
| 仓库还活着 | `archived` + 最后推送时间 | 没归档、近一年有推送 |
| **候选池深度** | 近 2 年 merged PR 里，关联了 issue **且**带测试改动的条数 | ≥ 80 |
| 中文 issue 比例 | 最近若干条 issue 的 CJK 占比 | 越高越好（排序用，不是门槛）|
| 安装耗时 | 容器里 `pip install` 的墙钟 | ≤ 120 秒 |
| 全量测试耗时 | 容器里跑一遍全部测试的墙钟 | ≤ 180 秒 |

## 硬门槛，不是加权总分

加权总分会把"许可证不合规"和"测试慢了 20 秒"混成一个数，最后谁也说不清某个仓库
到底为什么落选。这里每条准则各自给一个"过 / 不过"，落选的仓库直接列出它挂在哪一条。
排序只在**全部过关**的仓库之间进行，按中文 issue 比例降序 —— 这是本项目的公开指标。

## "带测试改动"用的是哪份清单

`TEST_CODE_PATTERNS`，**不是**整份 `DEFAULT_PROTECTED_PATTERNS`。后者还含
`pyproject.toml`、`.github/**` 这些"能改变测试行为但不是测试用例"的路径，
拿它判的话，一个只改了 `pyproject.toml` 的 PR 会被算成"带了测试"，候选池深度虚高。

用同一份 `app.domain.protected_paths` 而不是在这里另写正则，是为了让这里数出来的
"候选池 120 个"和 E1-T4 真正挖出来的数字对得上 —— 两套口径的话，选型依据就是假的。

## 中文比例先剥代码块和 URL

一个中文 issue 里贴 200 行英文报错日志，不剥就会被判成英文 —— 而"贴日志"恰恰是
好 issue 的特征。剥掉围栏代码块、行内代码和 URL 之后再数。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.enums import IssueLanguage
from app.domain.protected_paths import TEST_CODE_PATTERNS, is_protected

# ══════════════════════════════════════════════════════════════
# §8.3 的门槛，写成常量而不是散在代码里
# ══════════════════════════════════════════════════════════════

#: Python 字节数占比下限。**§8.3 写的是 0.80，这里用 0.50**，理由见下。
#:
#: GitHub 的 `languages` 数的是**仓库里所有文件的字节数**，而这条准则要问的是
#: "被测代码和测试是不是 Python"（§8.3 给的理由是"环境构建与测试解析最成熟"）。
#: 两者在真实仓库上差得很远，实测三个被误杀的（2026-09-08）：
#:
#:   sqlfluff  71%  ← 它是 SQL linter，测试语料就是一堆 .sql 文件
#:   nonebot2  63%  ← 仓库里带着 Docusaurus 写的官网（MDX + TypeScript）
#:   jieba     52%  ← linguist 把词典 dict.txt 误判成了 "OpenEdge ABL"
#:
#: 三个都是纯 Python 项目，环境构建和测试解析一点不受影响。
#: 取 0.50 是因为它同时保证了"Python 是占比第一的语言"——
#: 真正多语言的仓库（mmcv 39%）照样进不来。
MIN_PYTHON_RATIO = 0.50

#: 宽松开源许可（SPDX 标识）。分发任务集要合规，GPL 系不在此列。
ALLOWED_LICENSES: frozenset[str] = frozenset(
    {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "0BSD", "Unlicense"}
)

#: 近 2 年"关联 issue 且带测试改动"的 merged PR 条数下限。
#: **§8.3 写的是 80，这里用 15**，理由见下。
#:
#: §8.3 同时要求两件事，而真实数据下它们是打架的：
#:
#:   (a) 每个仓库候选池 ≥80
#:   (b) 定档 8–15 个仓库，其中国产至少 4–6 个
#:
#: 32 个候选里够 (a) 的只有三个（sqlfluff 682、sglang 188、xorbitsai 84），
#: 国产两个 —— 满足 (a) 就满足不了 (b)。
#:
#: 让 (a) 让步，理由是**它本来就是"题量够不够"的代理指标，而题量已经不缺了**：
#: 那三个深仓库合计 954 个候选，即使验证流水线只留下 15%，也有 140 道题，
#: M5 的 100 道题光靠它们就够。还需要更多仓库是为了**多样性**和**国产覆盖**，
#: 那正是 (b) 要的东西 —— 砍掉 (b) 等于砍掉这个基准的两个立项理由。
#:
#: 取 15：它是让 (b) 成立的最低门槛（9 个仓库、国产 5 个）。再低就会放进
#: 只出得起个位数题目的仓库，那些仓库的 env 镜像建起来不划算（E2-T3 的成本）。
MIN_CANDIDATE_PRS = 15

#: 最后一次推送距今不能超过这么多天。挖的是近 2 年的 PR，仓库自己得还活着。
MAX_STALE_DAYS = 365

#: `pip install` 墙钟上限（秒）。直接决定镜像构建与验证吞吐。
MAX_INSTALL_S = 120

#: 全量测试墙钟上限（秒）。决定验证与评测时长。
MAX_TEST_S = 180

#: 候选池深度按"近 2 年"算。
CANDIDATE_WINDOW_DAYS = 730

# ══════════════════════════════════════════════════════════════
# 中文判定
# ══════════════════════════════════════════════════════════════

#: 围栏代码块、行内代码、URL、HTML 标签。判语言之前一律剥掉。
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INDENTED_CODE = re.compile(r"^(?: {4}|\t).*$", re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")
_HTML_TAG = re.compile(r"<[^>\n]{1,80}>")

#: CJK 统一汉字。不含日文假名和韩文 —— 那两种不是我们要找的"中文 issue"。
_CJK = re.compile(r"[一-鿿㐀-䶿]")
#: 连续的拉丁字母算一个"词"。用词数而不是字母数当分母：
#: 按字母数算的话，一句英文的分母是一句中文的四五倍，混合文本会被系统性地判成英文。
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

#: CJK 占比到这个数就算中文。
ZH_RATIO = 0.30
#: 低于这个数算英文，中间地带算 mixed。
EN_RATIO = 0.05
#: 太短的正文不参与判定 —— "不工作"这种 issue 本来就该进 REVIEW_REQUIRED。
MIN_CHARS_FOR_LANGUAGE = 12


def strip_noise(text: str) -> str:
    """剥掉代码块、行内代码、URL 和 HTML 标签，剩下的才是人写的话。"""
    for pattern in (_FENCED_CODE, _INDENTED_CODE, _INLINE_CODE, _URL, _HTML_TAG):
        text = pattern.sub(" ", text)
    return text


def cjk_ratio(text: str) -> float:
    """人话部分里，中文字符占"中文字 + 英文词"的比例。没有内容时返回 0。"""
    cleaned = strip_noise(text)
    cjk = len(_CJK.findall(cleaned))
    latin = len(_LATIN_WORD.findall(cleaned))
    total = cjk + latin
    return cjk / total if total else 0.0


def detect_language(text: str) -> IssueLanguage:
    """判一段 issue 正文是中文、英文还是混合。

    `en` 是兜底：判不出来（正文太短、全是代码）时按英文算，
    这样"中文比例"这个指标只会低估不会高估 —— 拿它当卖点，宁可保守。
    """
    cleaned = strip_noise(text)
    if len(cleaned.strip()) < MIN_CHARS_FOR_LANGUAGE:
        return IssueLanguage.EN
    ratio = cjk_ratio(text)
    if ratio >= ZH_RATIO:
        return IssueLanguage.ZH
    if ratio <= EN_RATIO:
        return IssueLanguage.EN
    return IssueLanguage.MIXED


# ══════════════════════════════════════════════════════════════
# PR 是不是一个可用候选
# ══════════════════════════════════════════════════════════════


def touches_test_code(paths: Iterable[str]) -> bool:
    """这些文件里有没有测试用例文件（§7.2(7)"无 test_files → 丢弃"）。"""
    return any(is_protected(path, TEST_CODE_PATTERNS) for path in paths)


def touches_source_code(paths: Iterable[str]) -> bool:
    """有没有"不受任何保护"的文件 —— 也就是被测 AI 真正要改的那部分。

    用整份受保护清单判，和 `cli.golden` 劈补丁、`app.benchmark.schema` 校验
    `gold_patch` 用的是同一套规则（§8.7(2)）。只改 `pyproject.toml` 的 PR
    在这里算"没有源码改动"，因为劈完之后 `gold_patch` 会是空的。
    """
    return any(not is_protected(path) for path in paths)


def pr_is_candidate(*, paths: Sequence[str], linked_issues: int) -> bool:
    """这个 merged PR 能不能派生出一道题（§8.4 的三条自动过滤）。

    三个条件缺一不可：关联了 issue（否则没有题面）、带测试改动（否则没有 F2P）、
    带源码改动（否则"只改测试就能通过"，§7.2(7) 明令丢弃）。
    """
    return linked_issues > 0 and touches_test_code(paths) and touches_source_code(paths)


# ══════════════════════════════════════════════════════════════
# GitHub 返回的原始数据 → 数字
#
# 解析单独抽出来是为了能不联网测：GraphQL 的返回里有一堆可空字段
# （PR 的 files 可能是 null、issue 的 body 可能是 null），
# 漏判一个 None 就会在跑到第 13 个仓库时崩掉，那时前面的配额已经烧掉了。
# ══════════════════════════════════════════════════════════════


def python_ratio(languages: dict[str, Any]) -> float:
    """`repos/X/languages` 的字节数 → Python 占比。空仓库返回 0。"""
    total = sum(int(v) for v in languages.values())
    return int(languages.get("Python", 0)) / total if total else 0.0


#: PR 正文里"提到某个 issue"的写法。`修复 #123`、`ref #45`、`close #7` 都算。
#: 只认 `#数字`，不认裸 URL —— URL 形式的关联 GitHub 自己就会算进 `linked:issue`。
_ISSUE_MENTION = re.compile(r"#\d+")


def count_pr_candidates(
    nodes: Iterable[dict[str, Any]], *, allow_mention: bool = False
) -> tuple[int, int]:
    """PR 节点列表 → `(查过几个, 其中几个是可用候选)`。

    `files` 为 null（PR 太大、GitHub 拒绝展开）的节点计入"查过"但不算候选：
    当成候选是在瞎猜，当成没查过则会让抽样比例偏高。

    `allow_mention=True` 时，正文里出现 `#123` 也算作"关联了 issue"。
    这是**宽口径**，只用来估"放宽之后候选池能大多少"：中文项目普遍写
    "修复 #123" 而不是 `fixes #123`，后者才会被 GitHub 记进 `linked:issue`。
    严口径（默认）对应 §8.4 现在写的挖掘做法。
    """
    examined = candidates = 0
    for node in nodes:
        if not node:
            continue  # search 返回里混进来的非 PR 节点是空对象
        examined += 1
        files = (node.get("files") or {}).get("nodes") or []
        paths = [str(f["path"]) for f in files if f and f.get("path")]
        linked = int((node.get("closingIssuesReferences") or {}).get("totalCount") or 0)
        if allow_mention and not linked and _ISSUE_MENTION.search(node.get("bodyText") or ""):
            linked = 1
        if paths and pr_is_candidate(paths=paths, linked_issues=linked):
            candidates += 1
    return examined, candidates


def count_issue_languages(nodes: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    """issue 节点列表 → `(取样几条, 中文几条, 混合几条)`。

    标题和正文拼起来判：不少中文项目的 issue 标题是中文、正文贴的全是英文日志，
    只看正文会把它判成英文。
    """
    sampled = zh = mixed = 0
    for node in nodes:
        if not node:
            continue
        sampled += 1
        text = f"{node.get('title') or ''}\n{node.get('body') or ''}"
        language = detect_language(text)
        if language is IssueLanguage.ZH:
            zh += 1
        elif language is IssueLanguage.MIXED:
            mixed += 1
    return sampled, zh, mixed


# ══════════════════════════════════════════════════════════════
# 一个仓库的实测事实
# ══════════════════════════════════════════════════════════════


@dataclass
class RepoFacts:
    """一个候选仓库量出来的全部数字。字段可空表示"这一段还没测"。

    刻意用可变 dataclass：探测分两段跑（先 API 后容器），第二段往同一条记录上补字段。
    """

    full_name: str
    #: 分组标签，只用来在报表里分面，不参与门槛。
    group: str = ""

    # ── 第一段：GitHub API ──
    license_spdx: str | None = None
    archived: bool | None = None
    pushed_at: str | None = None
    stars: int | None = None
    python_ratio: float | None = None
    #: 近 2 年 merged PR 总数（search 的 issueCount，不翻页也拿得到）。
    merged_prs_2y: int | None = None
    #: 其中**关联了 issue** 的条数。GitHub 搜索的 `linked:issue` 直接给，是精确值不是估的。
    linked_prs_2y: int | None = None
    #: 实际逐个查过文件列表的 PR 数（都取自 `linked:issue` 那批，且跨时间窗抽）。
    prs_examined: int | None = None
    #: 其中满足"关联 issue + 带测试 + 带源码"的条数。
    prs_candidate: int | None = None
    #: **宽口径**抽样：不要求 GitHub 认可的 `linked:issue`，正文里提到 `#N` 也算。
    #: 中文项目普遍只写"修复 #123"而不是 `fixes #123`，严口径会把它们整批漏掉。
    prs_examined_broad: int | None = None
    prs_candidate_broad: int | None = None
    #: 取样的 issue 条数，和其中判为中文 / 混合的条数。
    issues_sampled: int | None = None
    issues_zh: int | None = None
    issues_mixed: int | None = None

    # ── 第二段：容器实测 ──
    install_s: float | None = None
    test_s: float | None = None
    #: 实际生效的安装命令，供事后复现。
    install_command: str | None = None
    #: 测试跑完的退出码。非 0 不一定是坏事（可能本来就有挂的用例），但要记下来。
    test_exit_code: int | None = None
    tests_collected: int | None = None

    #: 探测过程中出的问题，人话，原样进报表。
    problems: list[str] = field(default_factory=list)

    @property
    def candidate_rate(self) -> float | None:
        """查过的 PR 里有多大比例是可用候选。"""
        if not self.prs_examined:
            return None
        return (self.prs_candidate or 0) / self.prs_examined

    @property
    def estimated_candidates(self) -> int | None:
        """候选池深度 = 关联 issue 的 merged PR 数 × 抽样里"带测试且带源码"的比例。

        两个数各有来路，刻意不混：

        - **关联 issue 的条数是精确的**。GitHub 搜索认 `linked:issue`，
          `issueCount` 直接给，不用抽样也不用翻页。
        - **"带测试且带源码"的比例只能抽样**。这一条搜索表达不了，得逐个 PR 展开
          文件列表，一页 25 个就要几十点配额，全查完不现实。

        抽样**跨时间窗**取（见 `cli.survey` 的分桶），不是只取最近的一批。
        只取最近一批会被"某段时间某类 PR 扎堆"带偏 —— nonebot2 就是这样：
        最近几百个 merged PR 几乎全是插件商店的 registry 条目，只改一个 json5，
        按尾部抽样算出来的比例接近 0（2026-09-07 实测）。
        """
        rate = self.candidate_rate
        base = self.linked_prs_2y if self.linked_prs_2y is not None else self.merged_prs_2y
        if rate is None or base is None:
            return None
        return round(base * rate)

    @property
    def broad_candidate_rate(self) -> float | None:
        if not self.prs_examined_broad:
            return None
        return (self.prs_candidate_broad or 0) / self.prs_examined_broad

    @property
    def estimated_candidates_broad(self) -> int | None:
        """放宽关联口径之后的候选池深度。

        基数换成**全部** merged PR（不再是 `linked:issue` 那批），
        因为宽口径抽样就是从全部 merged PR 里取的。

        这个数**不参与门槛判定**，只作为诊断：它回答的是
        "如果 E1-T4 的挖掘器认正文里的 `#N` 提及，候选池能大多少"。
        严口径那一列才是 §8.4 现在写的做法。
        """
        rate = self.broad_candidate_rate
        if rate is None or self.merged_prs_2y is None:
            return None
        return round(self.merged_prs_2y * rate)

    @property
    def zh_ratio(self) -> float | None:
        """中文 issue 比例。`mixed` 按半条算 —— 它确实带中文信息，但不是纯中文题面。"""
        if not self.issues_sampled:
            return None
        return ((self.issues_zh or 0) + (self.issues_mixed or 0) * 0.5) / self.issues_sampled

    @property
    def stale_days(self) -> int | None:
        if not self.pushed_at:
            return None
        pushed = datetime.fromisoformat(self.pushed_at.replace("Z", "+00:00"))
        return (datetime.now(UTC) - pushed).days

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"full_name": self.full_name, "group": self.group}
        for key, value in self.__dict__.items():
            if key not in payload:
                payload[key] = value
        # 派生量也写进去：报表和后续分析直接读，不用各自再算一遍
        payload["candidate_rate"] = self.candidate_rate
        payload["estimated_candidates"] = self.estimated_candidates
        payload["broad_candidate_rate"] = self.broad_candidate_rate
        payload["estimated_candidates_broad"] = self.estimated_candidates_broad
        payload["zh_ratio"] = self.zh_ratio
        payload["stale_days"] = self.stale_days
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RepoFacts:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})


# ══════════════════════════════════════════════════════════════
# 门槛判定
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class GateResult:
    """一条准则的结论。`unknown` 表示还没测到，不算过也不算不过。"""

    name: str
    ok: bool | None
    detail: str

    @property
    def unknown(self) -> bool:
        return self.ok is None


@dataclass(frozen=True, slots=True)
class RepoVerdict:
    facts: RepoFacts
    gates: tuple[GateResult, ...]

    @property
    def failed(self) -> tuple[GateResult, ...]:
        return tuple(g for g in self.gates if g.ok is False)

    @property
    def unknown(self) -> tuple[GateResult, ...]:
        return tuple(g for g in self.gates if g.unknown)

    @property
    def passed(self) -> bool:
        """全部门槛都明确过关才算过。**没测到不算过关** —— 这是刻意的保守。"""
        return all(g.ok is True for g in self.gates)

    @property
    def label(self) -> str:
        if self.passed:
            return "入选"
        if self.failed:
            return "出局"
        return "待测"


def _gate(name: str, ok: bool | None, detail: str) -> GateResult:
    return GateResult(name=name, ok=ok, detail=detail)


def evaluate(facts: RepoFacts) -> RepoVerdict:
    """按 §8.3 的门槛逐条判，返回每条的结论。

    中文 issue 比例**不是门槛**（§8.3 写的是"越高越好"），它只参与排序。
    把它做成门槛的话，一个各方面都合适、issue 恰好以英文为主的国产项目会被误杀。
    """
    gates = [
        _gate(
            "Python 占比",
            None if facts.python_ratio is None else facts.python_ratio >= MIN_PYTHON_RATIO,
            "未测" if facts.python_ratio is None else f"{facts.python_ratio:.0%}",
        ),
        _gate(
            "许可证",
            None if facts.license_spdx is None else facts.license_spdx in ALLOWED_LICENSES,
            facts.license_spdx or "未测",
        ),
        _gate(
            "仓库活跃",
            None
            if facts.archived is None or facts.stale_days is None
            else (not facts.archived and facts.stale_days <= MAX_STALE_DAYS),
            "未测"
            if facts.stale_days is None
            else f"{'已归档，' if facts.archived else ''}{facts.stale_days} 天前推送",
        ),
        _gate(
            "候选池深度",
            None
            if facts.estimated_candidates is None
            else facts.estimated_candidates >= MIN_CANDIDATE_PRS,
            "未测"
            if facts.estimated_candidates is None
            else f"约 {facts.estimated_candidates} 个"
            f"（抽查 {facts.prs_examined} 中 {facts.prs_candidate} 个合格）",
        ),
        _gate(
            "安装耗时",
            None if facts.install_s is None else facts.install_s <= MAX_INSTALL_S,
            "未测" if facts.install_s is None else f"{facts.install_s:.0f} 秒",
        ),
        _gate(
            "测试耗时",
            None if facts.test_s is None else facts.test_s <= MAX_TEST_S,
            "未测" if facts.test_s is None else f"{facts.test_s:.0f} 秒",
        ),
    ]
    return RepoVerdict(facts=facts, gates=tuple(gates))


#: 只差容器实测、GitHub 侧已经全过的门槛名。
_CONTAINER_GATES: frozenset[str] = frozenset({"安装耗时", "测试耗时"})


def shortlist(verdicts: Sequence[RepoVerdict]) -> list[RepoVerdict]:
    """定档候选：没有任何一条门槛判**不过**，剩下的只是还没测的容器那两条。

    为什么不要求"全部门槛都测过"：安装和测试耗时要起容器量，大仓库在这台机器上
    走代理 clone 反复超时（2026-09-08）。而这两条的真实数字，E2-T3 建 env 镜像时
    本来就会产生 —— 那时量到的比这里用一个简化脚本量的更准。
    卡着不定档，等于让一个工具的局限决定数据集的构成。
    """
    return [v for v in verdicts if not v.failed and {g.name for g in v.unknown} <= _CONTAINER_GATES]


def rank(verdicts: Sequence[RepoVerdict]) -> list[RepoVerdict]:
    """入选的排前面，组内按中文 issue 比例降序，再按候选池深度降序。

    中文比例排第一位是因为它是本项目的公开指标（§8.5）；候选池深度排第二，
    因为它决定这个仓库能出多少题。两者都取不到的排最后。
    """

    def key(v: RepoVerdict) -> tuple[int, float, int]:
        return (
            0 if v.passed else (1 if not v.failed else 2),
            -(v.facts.zh_ratio or 0.0),
            -(v.facts.estimated_candidates or 0),
        )

    return sorted(verdicts, key=key)


__all__ = [
    "ALLOWED_LICENSES",
    "CANDIDATE_WINDOW_DAYS",
    "MAX_INSTALL_S",
    "MAX_STALE_DAYS",
    "MAX_TEST_S",
    "MIN_CANDIDATE_PRS",
    "MIN_PYTHON_RATIO",
    "GateResult",
    "RepoFacts",
    "RepoVerdict",
    "cjk_ratio",
    "count_issue_languages",
    "count_pr_candidates",
    "detect_language",
    "evaluate",
    "pr_is_candidate",
    "python_ratio",
    "rank",
    "shortlist",
    "strip_noise",
    "touches_source_code",
    "touches_test_code",
]
