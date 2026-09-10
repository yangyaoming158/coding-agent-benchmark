"""把挖掘候选组装成题目（E8-T2，`03-benchmark-spec.md` §8.4 第九步）。

E1-T4 挖出候选、E1-T5 洗干净并打了分，但**候选还不是题目**：它缺
`pass_to_pass`、缺 `environment_id`、缺 `difficulty`、缺 `content_hash`。
这个模块补齐前三样，第四样由 `TaskDefinition` 自己算。

## 为什么 P2P 要单独走一趟容器

§7.2(6) 把 P2P 定义成"在 `base + test_patch` 上就已经通过、打上 `gold_patch`
之后仍然通过的用例"。**这两句话都只能靠真跑一遍测试回答**，而 E1-T5 一个容器都不起，
所以它停在 `PRESCREENED`。

派生 P2P 的那两份报告来自验证流水线（`app.evaluation.validation`）的 S4 和 S7，
不是另写一套跑测试的代码 —— 理由见 §7.10「跑测试复用 `execute_tests`，没有第二套实现」。

## 组装和验证是两件事，中间不许互相插手

    组装（本模块）→ 题目定型、算出 content_hash → 八步验证（一个字都不改题目）

反过来"验证时顺手把 P2P 填上"是不行的：`content_hash` 是数据集快照的身份证（§7.5），
验证过程改题目定义，"同一个数据集版本"就不再成立。§7.10 拒绝过同样的做法
（不稳定用例只报不改），这里沿用。
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.benchmark.schema import P2PSampling, TaskDefinition
from app.domain.enums import IssueLanguage, TaskDifficulty
from app.domain.patch_paths import derive_patch_paths
from app.sandbox.images import EnvRecipe

#: 每条测试自己的超时（秒），靠 `pytest-timeout`（bench-base 里已经有）。
#:
#: **不加的话，一条挂住的测试会拖垮整轮。** 揭示 bug 的测试挂住是很正常的形状 ——
#: 2026-09-10 实测 `pallets/click#2775`：它修的就是"pager 子进程不退出"，
#: 新测试在 base 上卡在 `os.waitpid` 上不动，整个容器跑到 `test_timeout_s`（480 秒）
#: 被杀，题目被判 `TEST_TOO_SLOW` 丢掉 —— 而那是一道**好题**。
#: 加上之后那一轮 15.7 秒跑完，挂住的那条如实记成 FAILED，正是 F2P 该有的样子。
#:
#: 60 秒的取值：click 最慢的用例是毫秒级，60 秒只可能抓到真挂住的；
#: 而它远小于 `test_timeout_s`，所以挂住几条之后整轮仍然跑得完。
PER_TEST_TIMEOUT_S = 60

#: 跑测试的命令。§7.2(4) 的四条硬性要求都在这里：
#: 逐用例的机器可解析报告（`--junitxml`）、禁掉随机顺序和缓存、
#: 能在后面接用例 ID 列表、**没有** `-x`（fail fast 会让我们拿不到全部用例状态）。
#:
#: `--continue-on-collection-errors` 是 §7.2(4) 之外补的一条，理由同样是实测：
#: **pytest 默认一个文件收集出错就中断整轮**（`Interrupted: 1 error during collection`），
#: junit 里零条用例。而"新测试 import 了 gold 补丁才加的符号"是**再正常不过的题目形状** ——
#: 2026-09-10 实测 click 有两条候选栽在这里（#3637 import `PowerShellComplete`、
#: #3695 import `click.utils` 的新函数）。带上它之后 #3637 从 0 条变成 1848 条通过 + 1 个收集错误，
#: P2P 候选池完好，而那条收集不出来的测试如实记成 ERROR —— §7.2(5) 认 ERROR 是"在 base 上失败"。
#:
#: 它对正式评测同样是改善：被测 AI 的补丁要是弄坏了某个无关测试文件的 import，
#: 不带这个参数会拿到零条用例、判不出任何东西；带上就只坏那一个文件。
TEST_COMMAND_BASE = (
    "python -m pytest -rA -p no:randomly -p no:cacheprovider "
    "--continue-on-collection-errors --timeout={timeout} --junitxml={report}"
)

#: 报告落在工作区里的相对路径。**必须是相对路径**：执行器读的是
#: `workspace.path / test_report_path`（`app/evaluation/executor.py:406`），
#: 写成 `/tmp/report.xml` 的话 `Path` 的拼接规则会让它变成宿主机的 `/tmp/report.xml`。
#: §7.1 的示例写的是绝对路径，那是示例，落地口径以 Golden 题为准。
DEFAULT_REPORT_PATH = "report/junit.xml"

#: §7.7：全量套件跑一遍不超过这个秒数，P2P 就取全量通过用例，不抽样。
FULL_SUITE_BUDGET_S = 180

#: §7.7 的随机抽样条数。
RANDOM_SAMPLE_SIZE = 200

#: 抽样种子。**写死一个常量，不要每次随机** —— 种子是用来复现"当初选了哪 200 条"的，
#: 每次不一样就等于没记。
P2P_SEED = 20260910

#: §7.8 的难度分级阈值。
EASY_MAX_FILES, EASY_MAX_LINES = 1, 15
MEDIUM_MAX_FILES, MEDIUM_MAX_LINES = 3, 60

#: diff 里的改动行：`+` 或 `-` 开头，但不是文件头那两行（`+++` / `---`）。
_CHANGED_LINE = re.compile(r"^(?:\+(?!\+\+ )|-(?!-- ))", re.MULTILINE)


class AssemblyError(RuntimeError):
    """候选组装不成题目。附上人话原因，会进漏斗报表。"""


# ══════════════════════════════════════════════════════════════
# 候选
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条候选读出来之后的样子。

    读一次就脱离数据库：组装是纯函数，测试里不用起库就能验。
    """

    repo_name: str
    pr_number: int
    base_commit: str
    #: PR 合进的分支。click 有三分之一的 PR 合进 `stable` 维护分支，
    #: 而环境镜像的快照取的是默认分支顶端 —— 记下来是为了让淘汰原因分得开（§8.9 第六节）。
    base_ref_name: str
    issue_title: str
    issue_body: str
    issue_language: IssueLanguage
    #: 从 `test_patch` 抽出来的**候选** F2P，还没经过实测证伪（§8.10 第四节）。
    f2p_candidates: tuple[str, ...]
    test_patch: str
    gold_patch: str
    source_issue_url: str | None = None
    source_pr_url: str | None = None
    created_at_upstream: datetime | None = None
    prescreen_decision: str | None = None
    prescreen_score: float | None = None

    @property
    def task_id(self) -> str:
        """`{owner}__{repo}-{pr_number}`，和 SWE-bench 的命名兼容（§7.1）。"""
        owner, _, repo = self.repo_name.partition("/")
        return f"{owner}__{repo}-{self.pr_number}"


def load_candidate(payload: Mapping[str, Any], *, patch_root: Path) -> Candidate:
    """从 `task_candidates.raw_payload` + 磁盘上的补丁文件读出一条候选。

    补丁不在 `raw_payload` 里（§8.10 第三节），落在
    `var/mining/patches/<repo>/<pr>.{test,code}.patch`。这里两边合起来读，
    缺一个就当场报错 —— 缺补丁的候选组装不出题目，早报比晚报好查。
    """
    cleaned = payload.get("cleaned")
    if not isinstance(cleaned, Mapping):
        raise AssemblyError(
            "候选还没清洗过（raw_payload 里没有 cleaned），先跑 cli.prescreen clean"
        )

    pr = payload.get("pr")
    if not isinstance(pr, Mapping):
        raise AssemblyError("raw_payload 里没有 pr 段，候选不完整")

    repo_name = str(payload.get("repo") or "")
    if "/" not in repo_name:
        raise AssemblyError(f"raw_payload.repo 不是 owner/repo：{repo_name!r}")
    pr_number = int(pr["number"])

    base_commit = str(payload.get("base_commit") or "")
    if not base_commit:
        raise AssemblyError("候选没有 base_commit，`parents[0]` 没取到（§8.9 第五节）")

    owner, _, repo = repo_name.partition("/")
    stem = patch_root / f"{owner}__{repo}" / str(pr_number)
    test_patch = _read_patch(Path(f"{stem}.test.patch"))
    gold_patch = _read_patch(Path(f"{stem}.code.patch"))

    issues = payload.get("issues") or []
    issue_url = str(issues[0]["url"]) if issues and "url" in issues[0] else None

    return Candidate(
        repo_name=repo_name,
        pr_number=pr_number,
        base_commit=base_commit,
        base_ref_name=str(pr.get("base_ref_name") or ""),
        issue_title=str(cleaned.get("issue_title") or ""),
        issue_body=str(cleaned.get("issue_body") or ""),
        issue_language=IssueLanguage(str(cleaned.get("issue_language") or "en")),
        f2p_candidates=tuple(cleaned.get("f2p_candidates") or ()),
        test_patch=test_patch,
        gold_patch=gold_patch,
        source_issue_url=issue_url,
        source_pr_url=str(pr.get("url")) if pr.get("url") else None,
        created_at_upstream=_parse_time(pr.get("merged_at")),
        prescreen_decision=_prescreen_field(payload, "decision"),
        prescreen_score=_prescreen_score(payload),
    )


def _read_patch(path: Path) -> str:
    if not path.exists():
        raise AssemblyError(f"补丁文件不在：{path}（跑一次 cli.prescreen clean 重新生成）")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssemblyError(f"补丁文件是空的：{path}")
    return text


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _prescreen_field(payload: Mapping[str, Any], key: str) -> str | None:
    section = payload.get("prescreen")
    if not isinstance(section, Mapping):
        return None
    value = section.get(key)
    return None if value is None else str(value)


def _prescreen_score(payload: Mapping[str, Any]) -> float | None:
    raw = _prescreen_field(payload, "score")
    return None if raw is None else float(raw)


# ══════════════════════════════════════════════════════════════
# 环境规格
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Environment:
    """一个环境规格里题目要用到的那几样（`environment_specs` 的投影）。"""

    environment_id: str
    repo_name: str
    repo_url: str
    python_version: str
    install_command: str
    test_command: str
    test_report_path: str
    image_tag: str
    pre_test_command: str | None = None
    test_framework: str = "pytest"
    extra_protected_paths: tuple[str, ...] = ()


def environment_from_recipe(recipe: EnvRecipe) -> Environment:
    """把建镜像的配方（`images/envs/*.json`）翻成环境规格。

    配方是"这个镜像怎么建出来的"，环境规格是"用这个镜像怎么跑题目"，
    两者共用同一份事实：装依赖的命令、跑 pytest 的额外参数。
    E2-T3 在 `EnvRecipe.test_args` 的注释里明写这个字段"之后会变成
    `environment_specs.test_command` 的一部分（E8-T2）"，这里就是那一步。

    **一处已知的别扭，留给 E8-T3**：`test_args` 里如果带了目录
    （LLaMA-Factory 是 `--import-mode=importlib tests/ tests_v1/`），
    正式评测把用例 ID 接在后面时，pytest 会**既跑目录又跑指定用例**，
    等于跑了全量。判定结果仍然是对的（按 ID 从报告里查），只是跑得慢，
    §7.7 那个"只跑子集"的优化就没了。click 的 `test_args` 是空的，碰不到。
    """
    install = " && ".join(recipe.install_steps) if recipe.install_steps else "true"
    command = TEST_COMMAND_BASE.format(report=DEFAULT_REPORT_PATH, timeout=PER_TEST_TIMEOUT_S)
    if recipe.test_args:
        command = f"{command} {' '.join(recipe.test_args)}"
    return Environment(
        environment_id=recipe.environment_id,
        repo_name=recipe.repo_name,
        repo_url=recipe.repo_url,
        python_version=recipe.python_version,
        install_command=install,
        test_command=command,
        test_report_path=DEFAULT_REPORT_PATH,
        image_tag=recipe.image_tag,
    )


# ══════════════════════════════════════════════════════════════
# 难度（§7.8）
# ══════════════════════════════════════════════════════════════


def patch_size(patch: str) -> tuple[int, int]:
    """补丁改了几个文件、几行。

    行数只数真正的增删行：文件头那两行也以 `+++` / `---` 开头，
    按行首字符 grep 的话每个文件会白送两行，改动小的题会被算成大题。
    """
    return len(derive_patch_paths(patch)), len(_CHANGED_LINE.findall(patch))


def derive_difficulty(gold_patch: str) -> TaskDifficulty:
    """按 §7.8 从 `gold_patch` 的规模派生难度，不靠拍脑袋。

    `easy`：≤1 个文件且 ≤15 行；`medium`：≤3 个文件且 ≤60 行；其余 `hard`。
    """
    files, lines = patch_size(gold_patch)
    if files <= EASY_MAX_FILES and lines <= EASY_MAX_LINES:
        return TaskDifficulty.EASY
    if files <= MEDIUM_MAX_FILES and lines <= MEDIUM_MAX_LINES:
        return TaskDifficulty.MEDIUM
    return TaskDifficulty.HARD


# ══════════════════════════════════════════════════════════════
# F2P 派生（§7.2(5)）
# ══════════════════════════════════════════════════════════════

#: 算"这条测试揭示了 bug"的基线状态（§7.10 的 S5 用的是同一组）。
FAILING_ON_BASE = frozenset({"FAILED", "ERROR"})


@dataclass(frozen=True, slots=True)
class F2PSelection:
    """实测证伪之后的 `fail_to_pass`，连同被排除的那些一起带出来。"""

    ids: tuple[str, ...]
    #: 两份报告里都没有这个名字的候选。多半是 test_patch 只改了 fixture。
    unmatched: tuple[str, ...] = ()
    #: 打完 gold 才收集得出来、base 上收集不出来的候选。
    #:
    #: 典型形状：新测试 import 了 gold 补丁才加的符号，base 上整个测试文件收集失败。
    #: 这在挖掘出来的题里很常见，但**协议 C-12 明写 MISSING 既不算通过也不算失败**，
    #: 所以它当不了 F2P —— 验证流水线的 S5 会照样判 `F2P_NOT_FAILING`。
    #: 单独记一格是为了让"丢掉的是哪一类"看得见，而不是混进"报告里没有"里。
    uncollectable_on_base: tuple[str, ...] = ()
    #: 基线上就没失败的（候选 → 状态）。它们不揭示 bug，进 F2P 会让 Noop 哨兵非零。
    not_failing: dict[str, str] | None = None
    #: 基线挂了、打完 gold 还挂的。**不进 F2P 也不进 P2P** —— 它们在这个环境里
    #: 本来就是坏的，和这道题没关系。数量异常时值得人看一眼。
    not_fixed: tuple[str, ...] = ()
    #: ID 喂不回给 pytest 的，见 `round_trippable()`。
    dropped_unusable: tuple[str, ...] = ()


def expand_candidate(candidate_id: str, cases: Iterable[str]) -> list[str]:
    """把一条候选 F2P 展开成报告里真实存在的用例 ID。

    **参数化用例是这里的全部理由。** E1-T5 抽候选时只给基名
    （`tests/test_options.py::test_usage_show_choices`），而报告里是五条带参数的变体
    （`...[text choices]`、`...[int choices]`……）。§8.10 第四节当时写的是
    "靠 E4 的用例 ID 归一化去对"，但 `ParsedReport.resolve()` 的三层匹配
    （精确 / 备选 ID / 路径后缀）**没有这一层**，实测下来基名一律判 `MISSING`
    （2026-09-10，click 的 main 分支五条候选全军覆没）。

    修在这里而不是修 `resolve()`：一个基名对应 N 条状态可能各不相同的用例，
    `resolve()` 只能返回一条，硬选一条等于把 A 的结果安到 B 头上。
    题目里存**具体的**用例 ID，判定那一侧就永远是精确匹配 —— 协议 C-11
    把每条声明的 ID 当一条用例，基名本来就不是一条用例。
    """
    prefix = candidate_id + "["
    return sorted(c for c in cases if c == candidate_id or c.startswith(prefix))


def select_f2p(
    *,
    candidates: Sequence[str],
    baseline_status: Mapping[str, str],
    gold_status: Mapping[str, str],
) -> F2PSelection:
    """按 §7.2(5) 实测证伪：基线上必须失败、打完 gold 必须通过。

    两份状态表都来自全量报告，所以这里不起容器、是纯函数，能单测。
    """
    ids: list[str] = []
    unmatched: list[str] = []
    uncollectable: list[str] = []
    not_failing: dict[str, str] = {}
    not_fixed: list[str] = []
    unusable: list[str] = []

    for candidate in candidates:
        matched = expand_candidate(candidate, baseline_status)
        if not matched:
            # base 上没有、gold 上有 = 这条测试在 base 上收集不出来，不是"不存在"
            if expand_candidate(candidate, gold_status):
                uncollectable.append(candidate)
            else:
                unmatched.append(candidate)
            continue
        for case in matched:
            if baseline_status[case] not in FAILING_ON_BASE:
                not_failing[case] = baseline_status[case]
            elif gold_status.get(case) != "PASSED":
                not_fixed.append(case)
            elif not round_trippable(case):
                unusable.append(case)
            else:
                ids.append(case)

    return F2PSelection(
        ids=tuple(sorted(set(ids))),
        unmatched=tuple(unmatched),
        uncollectable_on_base=tuple(uncollectable),
        not_failing=not_failing or None,
        not_fixed=tuple(sorted(set(not_fixed))),
        dropped_unusable=tuple(sorted(set(unusable))),
    )


# ══════════════════════════════════════════════════════════════
# P2P 派生（§7.2(6) + §7.7）
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class P2PSelection:
    """一次 P2P 派生的结果，连同"为什么少了几条"一起带出来。"""

    ids: tuple[str, ...]
    sampling: P2PSampling
    #: 两边都通过、但报告里的 ID 喂不回给 pytest 的用例，见 `round_trippable()`。
    dropped_unusable: tuple[str, ...] = ()
    #: 基线通过、打完 gold 不通过的用例。**不进 P2P**，但要记下来：
    #: 数量异常说明 gold 动了很多行为，值得人看一眼。
    lost_after_gold: tuple[str, ...] = ()


def round_trippable(test_id: str) -> bool:
    """这条用例 ID 能不能原样喂回给 pytest。

    **2026-09-10 实测（click，2085 条用例里有 7 条不行）**：pytest 生成参数化用例 ID
    时会把非 ASCII 字符转义成 `\\u5b57` 这种文本，但它写 junit 报告时写的是真正的字
    `字`。于是从报告里读出来的 ID 拿回去跑，pytest 报
    `ERROR: not found`，**而且是 usage error（退出码 4）—— 整轮一条用例都不跑**。

    一条这样的 ID 混进 `pass_to_pass`，这道题以后每一次评测都会颗粒无收，
    表现却是"测试报告是空的"，非常难查。所以在组装阶段就滤掉，并记下滤了几条。

    判据是"全是 ASCII"：转义只发生在非 ASCII 字符上，ASCII 的 ID 两边一模一样。
    """
    return test_id.isascii()


#: 已知**不稳定**、一律不许进 `pass_to_pass` 的用例，按"测试函数名"匹配。
#:
#: 一条会飘的 P2P 不是回归护栏，是噪声：它会随机把一个正确的补丁判成 `UNRESOLVED`，
#: 而且不报错，只会让解决率莫名其妙偏低 —— AGENTS.md §5.5 说的就是这类问题。
#: §7.3 的 S8 早写了"剔除 flaky 用例"，这里是它的组装侧落地。
#:
#: **为什么按函数名整族剔，不按具体的参数化 ID**：2026-09-10 实测下来，飘的不是
#: 某一条，是整族 —— 同一道题连跑三遍，第一遍过、第二遍挂 `[test6-cat ]`、第三遍过；
#: 而一轮 22 题的门禁挂的是 `[test5-less]`。只剔观测到挂过的那两条，剩下 1121 条同族
#: 照样会飘（详见 `03-benchmark-spec.md` §7.11 第九节）。
FLAKY_TEST_FUNCTIONS: frozenset[str] = frozenset(
    {
        # click：`echo_via_pager()` 把内容写进 `less` / `cat` 的标准输入，而这一族里
        # 有几条用例喂的是"中途抛异常的生成器"，断言 pager 一个字都没收到。
        # 异常和 pager 的 flush 谁先谁后是调度决定的 —— 容器里 CPU 一紧就翻车。
        # 实测失败率约 0.16%，而 22 道题的 P2P 里有 1123 条，一轮门禁期望挂 1.8 条。
        "test_echo_via_pager",
    }
)


def function_name_of(test_id: str) -> str:
    """从用例 ID 里取测试函数名：`tests/a.py::TestX::test_y[p1-p2]` → `test_y`。

    **名字不能叫 `test_function_of`**：测试文件 import 它之后，pytest 会把这个
    模块级的 `test_*` 名字当成一条测试用例去收集，然后因为"缺 test_id 参数"报错。

    参数化的方括号和类名都去掉 —— 不稳定是**这个函数**的性质，
    不是某一组参数的性质。
    """
    after_path = test_id.rpartition("::")[2] or test_id
    return after_path.split("[", 1)[0]


def is_flaky(test_id: str) -> bool:
    """这条用例属不属于已知不稳定的那几族。"""
    return function_name_of(test_id) in FLAKY_TEST_FUNCTIONS


def same_module_cases(cases: Iterable[str], gold_patch: str) -> set[str]:
    """和 `gold_patch` 改动文件同模块的用例（§7.7 抽样策略的第一半）。

    判据：拿 gold 改动的每个文件的主文件名（`src/click/types.py` → `types`），
    看用例所在的测试文件路径里有没有它（`tests/test_types/test_Path.py` 命中）。

    这是个近似 —— 测试文件叫什么名字是仓库自己的习惯，没有通用规则。
    近似偏松比偏严好：多选几条 P2P 只是多跑一会儿，少选了就是回归护栏漏了。
    """
    stems = {Path(p).stem for p in derive_patch_paths(gold_patch)}
    stems.discard("")
    hits: set[str] = set()
    for case in cases:
        path = case.split("::", 1)[0]
        if any(stem in path for stem in stems):
            hits.add(case)
    return hits


def select_p2p(
    *,
    baseline_passing: Iterable[str],
    gold_passing: Iterable[str],
    fail_to_pass: Sequence[str],
    suite_seconds: float,
    gold_patch: str,
    seed: int = P2P_SEED,
) -> P2PSelection:
    """按 §7.2(6) 和 §7.7 选出 `pass_to_pass`。

    候选池 = **基线通过 ∩ 打完 gold 仍然通过**，再减去 F2P、减去喂不回给 pytest 的
    （`round_trippable`）、减去已知会飘的（`is_flaky`）。

    交集这一步不能省。只用基线那一半的话，凡是 gold 顺带改了行为的用例都会在
    验证流水线的 S8 被记成 `GOLD_REGRESSION` —— **整道好题被丢掉，而且理由是错的**：
    gold 没有回归，是我们把不该当护栏的用例塞进了护栏。
    """
    baseline = {c for c in baseline_passing if c not in set(fail_to_pass)}
    gold = set(gold_passing)
    lost = sorted(baseline - gold)

    both = baseline & gold
    # 两道过滤，理由不同但处置一样：进不了 P2P，且要记下来滤了几条。
    #   round_trippable  —— 这条 ID 喂回给 pytest 它不认（§8.11 第五节）
    #   is_flaky         —— 这条用例本身会飘，当不了回归护栏
    unusable = sorted(c for c in both if not round_trippable(c) or is_flaky(c))
    pool = sorted(c for c in both if round_trippable(c) and not is_flaky(c))

    if suite_seconds <= FULL_SUITE_BUDGET_S:
        # 套件跑得起全量，就不抽样 —— 护栏越全越好（§7.7 第一条）
        sampling = P2PSampling(strategy="full", seed=None, total_pool=len(pool))
        chosen = pool
    else:
        module_hits = same_module_cases(pool, gold_patch)
        rest = [c for c in pool if c not in module_hits]
        rng = random.Random(seed)
        extra = rng.sample(rest, min(RANDOM_SAMPLE_SIZE, len(rest)))
        chosen = sorted(module_hits | set(extra))
        sampling = P2PSampling(strategy="module_and_random", seed=seed, total_pool=len(pool))

    return P2PSelection(
        ids=tuple(chosen),
        sampling=sampling,
        dropped_unusable=tuple(unusable),
        lost_after_gold=tuple(lost),
    )


# ══════════════════════════════════════════════════════════════
# 组装
# ══════════════════════════════════════════════════════════════


def assemble(
    candidate: Candidate,
    environment: Environment,
    *,
    dataset_id: str,
    fail_to_pass: Sequence[str] | None = None,
    pass_to_pass: Sequence[str] = (),
    p2p_sampling: P2PSampling | None = None,
    agent_timeout_s: int = 720,
    test_timeout_s: int = 480,
) -> TaskDefinition:
    """候选 + 环境 → `TaskDefinition`。

    构造时 `TaskDefinition` 会把 §7 和协议的规则挨条查一遍（test_patch 只能碰测试
    文件、gold_patch 不能命中受保护路径、issue 不能泄题……），所以这一步同时就是校验；
    不合格的候选在这里抛 `ValidationError`，进不了下一步。

    `fail_to_pass` 不给就用候选里那份**未经证伪**的清单 —— 探测轮就是拿它去问
    "这些在基线上真的都挂吗"。定稿的题目要传实测确认过的那份。
    """
    ids = list(fail_to_pass if fail_to_pass is not None else candidate.f2p_candidates)
    if not ids:
        raise AssemblyError(f"{candidate.task_id}：一条候选 F2P 都没有，组装不出题目")

    # 不稳定用例在**这里**兜底剔除，而不是只在 `select_p2p()` 里。
    #
    # 两条路都会走到 assemble()：探测轮现算 P2P（走 select_p2p），组装轮读探测轮
    # 缓存下来的那份清单（不走 select_p2p）。只在 select_p2p 里过滤的话，
    # 缓存那条路会把老规则的结果原样带进题目 —— 2026-09-10 实测踩到：
    # 加完过滤重跑 assemble，"更新 22"，而 1123 条 pager 用例一条没少。
    #
    # "一条会飘的用例不许当回归护栏"是**题目的性质**，不是某一条派生路径的性质，
    # 所以判据要放在产出 TaskDefinition 的这一步。
    p2p = [case for case in pass_to_pass if not is_flaky(case)]
    dropped_flaky = len(pass_to_pass) - len(p2p)
    if dropped_flaky and p2p_sampling is not None and p2p_sampling.strategy == "full":
        # `full` 的定义就是"候选池全收"，池子小了这个数要跟着小，
        # 不然 §7.7 要求记录的 total_pool 和实际清单对不上
        p2p_sampling = p2p_sampling.model_copy(update={"total_pool": len(p2p)})

    tags = {"bugfix", "mined", dataset_id}
    if candidate.base_ref_name:
        # 维护分支上的题单独打一个标：物化和装依赖能不能对得上是分开的风险（§8.9 第六节）
        tags.add(f"branch-{candidate.base_ref_name}")

    _, _, repo = candidate.repo_name.partition("/")

    return TaskDefinition(
        task_id=candidate.task_id,
        dataset_id=dataset_id,
        repo_url=environment.repo_url,
        repo_name=candidate.repo_name,
        base_commit=candidate.base_commit,
        environment_id=environment.environment_id,
        issue_title=candidate.issue_title,
        issue_body=candidate.issue_body,
        issue_language=candidate.issue_language,
        install_command=environment.install_command,
        pre_test_command=environment.pre_test_command,
        test_command=environment.test_command,
        test_framework=environment.test_framework,
        test_report_path=environment.test_report_path,
        test_patch=candidate.test_patch,
        fail_to_pass=ids,
        pass_to_pass=p2p,
        p2p_sampling=p2p_sampling,
        gold_patch=candidate.gold_patch,
        agent_timeout_s=agent_timeout_s,
        test_timeout_s=test_timeout_s,
        source_issue_url=candidate.source_issue_url,
        source_pr_url=candidate.source_pr_url,
        created_at_upstream=candidate.created_at_upstream,
        language="python",
        framework=repo or None,
        difficulty=derive_difficulty(candidate.gold_patch),
        tags=sorted(tags),
    )
