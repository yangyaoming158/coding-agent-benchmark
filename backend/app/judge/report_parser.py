"""测试报告解析器（E4-T1，`06-judge-attribution.md` §11.3）。

把测试跑完留下的东西变成 `{用例 ID: 状态}`。判定引擎（E4-T3）拿它去算 F2P / P2P。

    report/junit.xml  ──首选──▶ ┐
                                ├─▶ ParsedReport ─▶ E4-T3 判定
    stdout / stderr   ──兜底──▶ ┘

## 三条硬性要求

1. **完全确定。** 同一份报告解析两次，逐字段必须相同（`AGENTS.md` §5.1）。
   所以这里没有任何依赖时间、随机数、字典遍历顺序或文件系统的地方；
   遇到重复、冲突、猜不准的情况，一律用**写死的优先级**裁决，不是"看情况"。
2. **禁止把 MISSING / SKIPPED / XFAIL 当通过**（协议 C-12）。本模块只负责如实
   报出状态，不做任何"约等于通过"的合并。
3. **要把 C-13b 三项自检需要的信息交出来。** 出现 `MISSING` 时协议要求先自检
   再分支，判错方向就会把平台自己的 bug 算到被测 AI 头上（C-13a）。
   `ParsedReport.check_integrity()` 就是这三项。

## 状态怎么判（pytest 9.1.1 实测，2026-09-05）

| junitxml 里长什么样 | 判成 |
|:---|:---|
| 没有 failure / error / skipped 子元素 | `PASSED` |
| `<failure>` | `FAILED` |
| `<failure message="[XPASS(strict)] …">` | `XPASS` |
| `<error>` | `ERROR` |
| `<skipped type="pytest.skip">` | `SKIPPED` |
| `<skipped type="pytest.xfail">` | `XFAIL` |
| `classname=""` + `<error message="collection failure">` | 收集错误，不是用例 |

两个反直觉的地方，都实测确认过：

- **测试函数体里 `raise RuntimeError` 是 `<failure>`，不是 `<error>`。**
  只有 fixture / setup / teardown 里抛异常和收集失败才是 `<error>`。
  协议 C-10 的 `ERROR` 对应的正是后两种。
- **`<system-out>` / `<system-err>` 不是状态标记。** 开了 `junit_logging`
  之后通过的用例也会有子元素，所以"有子元素就不是 PASSED"是错的，
  必须按标签名认。

## XPASS 的坑：junitxml 表达不了它

非 strict 的 `xfail` 用例真的通过时，pytest 在 XML 里写的是一个**没有子元素的
普通 testcase**，和 PASSED 一模一样。协议 C-10 要求 XPASS 是独立状态，
但 junitxml 给不出来。

这里**不装作能分出来**：

- 只有 XML 可用时，非 strict 的 XPASS 会被如实地报成 `PASSED`，
  并把 `ParsedReport.xpass_may_read_as_passed` 置为 True，让上层知道这一路数据有这个盲区。
- `strict=True` 的 XPASS 是例外，它两边都被算成失败（XML 写
  `<failure message="[XPASS(strict)] …">`，文本的逐条行直接打 `FAILED`，
  标记只出现在短摘要行里）。`_with_strict_xpass_marker()` 是纠正它的**唯一**一处规则，
  XML 和文本共用。
- 文本输出反而分得清（`… XPASS (already fixed)`）。所以两边都有时，
  用文本把 `PASSED` 升级成 `XPASS` —— **只升这一种**，其余一律以 XML 为准。

## 文本兜底能兜住什么

junitxml 没生成时（容器被杀、test_command 写错）只能从 stdout 里捞。两种格式：

    tests/test_a.py::test_x PASSED                    [ 33%]   ← `-v` 的逐条行
    PASSED tests/test_a.py::test_x                              ← `-rA` 的短摘要

**两种都要靠参数才有**（`-v` / `-rA`）。默认输出的进度点（`..FFEsxX`）里没有用例 ID，
但短摘要那一节默认就打印失败和错误 —— 实测 15 条用例的默认 `-q` 输出能捞到 4 条，
**8 条通过的一条也看不见**。

所以文本兜底天生是残缺的，`check_integrity()` 一律把它记成"报告不完整"。
捞不到的用例宁可报不出来，也不猜：编出来的状态比没有状态危险得多。

短摘要还有一个坑：`SKIPPED [1] tests/test_a.py:26: not today` 给的是**文件:行号**，
拿不到用例 ID。这种行只计数（`skipped_without_id`），不猜 ID。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.domain.enums import TestStatus
from app.judge.test_ids import (
    CaseId,
    id_node,
    id_path,
    junit_case_id,
    module_path_from_dotted,
    normalize_test_id,
)

#: 摘要最长多少字符。和 `test_results.message_excerpt` 的列宽（String(2000)）对齐——
#: 超了会在入库时报 DataError，而那时报告已经解析完、容器已经销毁，重来一次很贵。
MAX_MESSAGE_EXCERPT = 2000

#: strict 的 xfail 用例通过时，pytest 在 `<failure>` 的 message 里打的标记。
_XPASS_STRICT_MARKER = "[XPASS(strict)]"

#: 文本输出里的状态词。顺序无关，但必须和 `TestStatus` 一一对上。
_TEXT_STATUSES: Mapping[str, TestStatus] = {
    "PASSED": TestStatus.PASSED,
    "FAILED": TestStatus.FAILED,
    "ERROR": TestStatus.ERROR,
    "SKIPPED": TestStatus.SKIPPED,
    "XFAIL": TestStatus.XFAIL,
    "XPASS": TestStatus.XPASS,
}
_STATUS_WORDS = "|".join(_TEXT_STATUSES)

#: `-v` 逐条行里用例 ID 后面那一截：状态词 + 可选的 `(原因)` + 可选的 `[ 33%]`。
#: 收得这么紧是为了不误伤 traceback —— 松一点就会把测试自己打印的内容当成结果。
_VERBOSE_TAIL = re.compile(rf"^({_STATUS_WORDS})\b\s*(\([^\n]*\))?\s*(\[\s*\d+%\])?\s*$")

#: `-rA` 短摘要行：状态词开头。
_SUMMARY_LINE = re.compile(rf"^({_STATUS_WORDS})\s+(\S.*)$")

#: 短摘要那一节的开头。只在这一节里认摘要行，免得把被测代码打印的同形状文本当成结果。
_SUMMARY_HEADER = "short test summary info"

#: 短摘要里 SKIPPED 行的形状：`SKIPPED [1] tests/test_a.py:26: reason`，没有用例 ID。
_SKIPPED_COUNT_PREFIX = re.compile(r"^\[\d+\]\s")

#: 截断的 XML 补上这些闭合标签试试能不能救回来，按顺序试，第一个成功的算数。
_CLOSING_ATTEMPTS = ("</testsuite></testsuites>", "</testsuite>", "</testsuites>", "")


class ReportSource(StrEnum):
    """这份结果是从哪儿解析出来的。归因和复核任务要靠它解释"为什么信息这么少"。"""

    #: junitxml 完整解析成功。
    JUNIT_XML = "JUNIT_XML"
    #: junitxml 被截断，靠补闭合标签救回了前半截。**报告不完整**，按 C-13b 第 1 项处理。
    JUNIT_XML_SALVAGED = "JUNIT_XML_SALVAGED"
    #: 没有可用的 XML，从 stdout / stderr 文本里捞出来的。
    STDOUT = "STDOUT"
    #: 什么都没捞到。
    NONE = "NONE"


#: 除了完整的 junitxml，其余来源都算"报告不完整"（C-13b 第 1 项），各自的说法。
#:
#: 文本兜底为什么也算不完整：pytest 默认只在短摘要里打印**失败和错误**，
#: 通过的用例一条都不打（要加 `-rA` 才有）。于是"这条用例没出现在报告里"
#: 既可能是它真没跑，也可能是它通过了但没被打印出来 —— 分不出来。
#: 这时候如果记 MISSING 再去罚 AI，罚的其实是我们自己的 test_command 少写了参数。
_INCOMPLETE_REASONS: Mapping[ReportSource, str] = {
    ReportSource.JUNIT_XML_SALVAGED: "junitxml 被截断，只救回了一部分用例",
    ReportSource.STDOUT: "没有 junitxml，只能从文本兜底；通过的用例可能整批没被打印出来",
    ReportSource.NONE: "既没有可解析的 junitxml，也没能从文本里捞到任何用例",
}


@dataclass(frozen=True, slots=True)
class TestCaseResult:
    """一条用例的结果。字段和 `test_results` 表的列一一对应。"""

    test_id: str
    status: TestStatus
    #: 耗时，毫秒。文本兜底拿不到，为 None。
    duration_ms: int | None
    #: 失败/错误信息节选，已截到 `MAX_MESSAGE_EXCERPT`。通过的用例为 None。
    message_excerpt: str | None


@dataclass(frozen=True, slots=True)
class CollectionError:
    """收集阶段就挂了的模块（import 报错之类），一条用例都没跑起来。

    **这不等于被测 AI 干的。** 可能是它改坏了 import，也可能是题目本身坏了。
    分不出来，所以这里只如实记录，分支交给 E4-T3 按协议 C-13 判（见 `IntegrityCheck`）。
    """

    module_path: str
    message_excerpt: str | None


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    """协议 C-13b 要求的三项自检结果。

    出现 `MISSING` 时**必须先看这个再分支**，顺序反了就会把平台自己的 bug
    算到被测 AI 头上（C-13a 明确禁止仅凭 MISSING 判作弊）。三项分别是：

    1. 报告文件是否完整生成 → `report_complete` / `report_problem`。
       **只有完整解析成功的 junitxml 才算完整**：截断的、以及从文本兜底来的都不算，
       因为这两种都会漏掉用例，而漏掉的用例看起来和"AI 把测试删了"一模一样
    2. 用例 ID 归一化是否正确 → `reported_ids` 和 `missing_ids` 摆在一起对照，
       外加 `near_misses` 直接指出疑似没对上的那几对
    3. 收集阶段有没有报错 → `collection_error_modules`
    """

    report_complete: bool
    report_problem: str | None
    #: 题目里列了、报告里没找到的用例。
    missing_ids: tuple[str, ...]
    #: 报告里实际出现过的所有 ID（排序后），给人对照用。
    reported_ids: tuple[str, ...]
    #: 疑似归一化没对上的：{题目里的 ID: 报告里那条长得几乎一样的 ID}。
    #: 非空基本就是解析器的锅，按 C-13 的 (a) 分支走。
    near_misses: Mapping[str, str]
    collection_error_modules: tuple[str, ...]

    @property
    def blames_harness(self) -> bool:
        """三项自检里有没有**明确指向平台自己**的证据。

        为 True 时按 C-13 的 (a) 分支：`FAILED` + `HARNESS_ERROR`，
        `agent_outcome = NULL`，计入平台故障率，**不罚 AI**。

        收集错误**不算**在内：它既可能是 AI 改坏了 import（分支 b），
        也可能是题目坏了（分支 a），这里分不出来。那一步归 E4-T3，
        它手里有补丁改了哪些文件的信息，能拿 C-13c 的实际证据去判。
        """
        return not self.report_complete or bool(self.near_misses)


@dataclass(frozen=True, slots=True)
class ParsedReport:
    """一份测试报告解析出来的全部东西。

    `cases` 的键是归一化之后的用例 ID。想按题目里的 ID 取结果，用 `resolve()`，
    别直接下标 —— `resolve()` 还会试备选 ID 和路径后缀，那正是防假 `MISSING` 的地方。
    """

    cases: Mapping[str, TestCaseResult]
    source: ReportSource
    #: 备选 ID → 主 ID。classname 有歧义时（xunit2 没有 `file` 属性）会有内容。
    aliases: Mapping[str, str]
    collection_errors: tuple[CollectionError, ...]
    #: 报告被截断、只救回了一部分。
    truncated: bool
    #: 报告本身有问题时的人话说明（文件不存在、解析失败、被截断），没问题时为 None。
    problem: str | None
    #: 短摘要里拿不到用例 ID 的 SKIPPED 行有几条。不为 0 说明这份文本兜底不完整。
    skipped_without_id: int
    #: 这份结果里的 `PASSED` 有可能其实是非 strict 的 XPASS —— junitxml 表达不了它，
    #: 而这次又没有文本输出可以补。上层要在报表里如实标注，不能当成"确认通过"。
    xpass_may_read_as_passed: bool

    @property
    def statuses(self) -> dict[str, TestStatus]:
        """`{用例 ID: 状态}`，§11.3 里 `TestReportParser.parse` 写的那个形状。"""
        return {test_id: case.status for test_id, case in self.cases.items()}

    def resolve(
        self, test_id: str, *, repo_root: Path | str | None = None
    ) -> TestCaseResult | None:
        """按题目里写的 ID 取结果，取不到返回 None（调用方据此记 `MISSING`）。

        三层，按顺序试，**第一层命中就停**：

        1. 归一化之后精确相等
        2. 命中某条用例的备选 ID（classname 切分歧义造成的）
        3. 路径后缀相同、节点部分完全相同 —— 报告里是绝对路径而调用方没给
           `repo_root` 时靠这层兜住

        第 3 层**要求全局唯一**：两条报告用例的路径都以它结尾时返回 None。
        猜错一条的后果是把 A 的结果安到 B 头上，那比记一条 `MISSING` 严重得多。
        """
        wanted = normalize_test_id(test_id, repo_root=repo_root)
        if not wanted:
            return None
        if wanted in self.cases:
            return self.cases[wanted]
        alias = self.aliases.get(wanted)
        if alias is not None:
            return self.cases.get(alias)

        node = id_node(wanted)
        suffix = "/" + id_path(wanted)
        hits = [
            case
            for case in self.cases.values()
            if id_node(case.test_id) == node and id_path(case.test_id).endswith(suffix)
        ]
        return hits[0] if len(hits) == 1 else None

    def check_integrity(
        self, expected_ids: Iterable[str], *, repo_root: Path | str | None = None
    ) -> IntegrityCheck:
        """跑协议 C-13b 的三项自检。出现 `MISSING` 时由 E4-T3 拿去分支。"""
        missing = tuple(
            normalize_test_id(t, repo_root=repo_root)
            for t in expected_ids
            if self.resolve(t, repo_root=repo_root) is None
        )
        complete = self.source is ReportSource.JUNIT_XML and not self.truncated
        return IntegrityCheck(
            report_complete=complete,
            report_problem=self.problem or (None if complete else _INCOMPLETE_REASONS[self.source]),
            missing_ids=missing,
            reported_ids=tuple(sorted(self.cases)),
            near_misses=self._near_misses(missing),
            collection_error_modules=tuple(e.module_path for e in self.collection_errors),
        )

    def _near_misses(self, missing_ids: Sequence[str]) -> dict[str, str]:
        """给每条 `MISSING` 找一条"节点部分一样、只有路径不同"的报告用例。

        找得到就说明归一化没把两边收敛到一起 —— 这正是 C-13b 第 2 项要人看的东西，
        与其让人自己拿两列 ID 肉眼比对，不如直接把嫌疑对指出来。

        同一条 `MISSING` 有多个候选时取排序后的第一个：只是给人看的线索，
        不参与判定，取谁都不影响结论，但必须**每次取一样的**。
        """
        by_node: dict[str, list[str]] = {}
        for test_id in self.cases:
            by_node.setdefault(id_node(test_id), []).append(test_id)
        found = {}
        for wanted in missing_ids:
            candidates = by_node.get(id_node(wanted))
            if candidates:
                found[wanted] = min(candidates)
        return found


class TestReportParser(Protocol):
    """§11.3 定的解析器接口。pytest 之外的框架（unittest、jest）照这个实现。

    §11.3 里写的返回值是 `dict[str, TestStatus]`，这里返回 `ParsedReport`——
    协议 C-13b 要求出现 `MISSING` 时自检"报告是否完整、ID 归一化对不对、
    有没有收集错误"，光给一个状态字典交不出这些信息。
    要那个字典形状的话取 `ParsedReport.statuses`。
    """

    def parse(
        self, report_path: Path | None, stdout: str, stderr: str
    ) -> ParsedReport: ...  # pragma: no cover - 接口声明


def _with_strict_xpass_marker(status: TestStatus, message: str | None) -> TestStatus:
    """带 `[XPASS(strict)]` 标记的"失败"其实是 XPASS。

    `strict=True` 的 xfail 用例真的通过时，pytest 两边都把它算成失败：
    XML 写 `<failure message="[XPASS(strict)] …">`，文本的逐条行直接打 `FAILED`
    （只有短摘要那行带标记）。协议 C-10 要求 XPASS 是独立状态，所以两边都按这条规则纠正。

    **规则只此一份**：XML 和文本各写一遍的话，同一次运行从两条路解析会得到不同状态，
    而判定必须完全确定（`AGENTS.md` §5.1）。
    """
    if status is TestStatus.FAILED and message and message.startswith(_XPASS_STRICT_MARKER):
        return TestStatus.XPASS
    return status


def _excerpt(element: ET.Element) -> str | None:
    """从 `<failure>` / `<error>` / `<skipped>` 里取一段摘要，截到列宽以内。"""
    parts = [
        text
        for text in ((element.get("message") or "").strip(), (element.text or "").strip())
        if text
    ]
    if not parts:
        return None
    text = "\n".join(parts)
    if len(text) <= MAX_MESSAGE_EXCERPT:
        return text
    return text[: MAX_MESSAGE_EXCERPT - 1] + "…"


def _duration_ms(element: ET.Element) -> int | None:
    raw = element.get("time")
    if raw is None:
        return None
    try:
        return round(float(raw) * 1000)
    except ValueError:
        return None


def _status_of(testcase: ET.Element) -> tuple[TestStatus, str | None]:
    """按子元素定状态。

    **按文档顺序取第一个状态子元素。** 一条用例可能同时有 `<failure>`（call 阶段挂了）
    和 `<error>`（teardown 又挂了），pytest 按阶段顺序写，取第一个就是"测试本身
    先出的问题"，这也是我们要报的。取最后一个会把 teardown 的噪声盖在真实原因上。
    """
    for child in testcase:
        if child.tag == "failure":
            return _with_strict_xpass_marker(TestStatus.FAILED, child.get("message")), _excerpt(
                child
            )
        if child.tag == "error":
            return TestStatus.ERROR, _excerpt(child)
        if child.tag == "skipped":
            kind = child.get("type") or ""
            status = TestStatus.XFAIL if kind == "pytest.xfail" else TestStatus.SKIPPED
            return status, _excerpt(child)
    return TestStatus.PASSED, None


def _is_collection_error(testcase: ET.Element) -> bool:
    """收集失败的条目：`classname` 是空的，且带 `<error>`。

    实测形状（pytest 9.1.1）：
    `<testcase classname="" name="brk.test_broken"><error message="collection failure">`。
    正常用例即使在仓库根目录下，classname 也是模块名（`test_a`），不会为空。
    """
    return not (testcase.get("classname") or "") and testcase.find("error") is not None


def _salvage_truncated(text: str) -> ET.Element | None:
    """截断的 XML 尽量救回前半截。

    容器被 OOM 杀掉或者超时，junitxml 就会写一半。整份丢掉的话，
    "10 条里有 5 条通过了"和"一条都没跑"就分不出来了，而这两种在归因上完全不同。

    做法：先退到最后一个完整的 `</testcase>` 或自闭合的 `/>`，再挨个试补闭合标签。
    试的顺序写死，所以同一份坏文件每次都救回同样多的内容。
    """
    ends = [text.rfind("</testcase>") + len("</testcase>"), text.rfind("/>") + len("/>")]
    for cut in sorted((e for e in ends if e > 0), reverse=True):
        for closing in _CLOSING_ATTEMPTS:
            try:
                return ET.fromstring(text[:cut] + closing)
            except ET.ParseError:
                continue
    return None


def _register(
    cases: dict[str, TestCaseResult],
    aliases: dict[str, str],
    case_id: CaseId,
    status: TestStatus,
    duration_ms: int | None,
    message_excerpt: str | None,
) -> None:
    """收一条用例结果。

    **同一个 ID 出现两次时先来的赢。** 正常报告里不会重复，重复只可能来自
    重跑插件之类。先来后到是个写死的规则，重要的不是选哪个，而是每次都选同一个。
    """
    if case_id.primary in cases:
        return
    cases[case_id.primary] = TestCaseResult(
        test_id=case_id.primary,
        status=status,
        duration_ms=duration_ms,
        message_excerpt=message_excerpt,
    )
    for alternate in case_id.alternates:
        # 备选 ID 绝不能盖住任何一条真实用例的主 ID
        if alternate not in cases:
            aliases.setdefault(alternate, case_id.primary)


def parse_junit_xml(text: str, *, repo_root: Path | str | None = None) -> ParsedReport:
    """解析 junitxml。xunit1 和 xunit2 两种 family 都吃。"""
    truncated = False
    problem: str | None = None
    try:
        root: ET.Element | None = ET.fromstring(text)
    except ET.ParseError as exc:
        root = _salvage_truncated(text)
        truncated = True
        problem = f"junitxml 解析失败（{exc}）" + ("，救回了前半截" if root is not None else "")

    if root is None:
        return ParsedReport(
            cases={},
            source=ReportSource.NONE,
            aliases={},
            collection_errors=(),
            truncated=True,
            problem=problem or "junitxml 解析失败，一条也没救回来",
            skipped_without_id=0,
            xpass_may_read_as_passed=False,
        )

    cases: dict[str, TestCaseResult] = {}
    aliases: dict[str, str] = {}
    collection_errors: list[CollectionError] = []
    for testcase in root.iter("testcase"):
        if _is_collection_error(testcase):
            error = testcase.find("error")
            collection_errors.append(
                CollectionError(
                    module_path=module_path_from_dotted(testcase.get("name") or ""),
                    message_excerpt=_excerpt(error) if error is not None else None,
                )
            )
            continue
        status, message = _status_of(testcase)
        _register(
            cases,
            aliases,
            junit_case_id(
                testcase.get("classname") or "",
                testcase.get("name") or "",
                testcase.get("file"),
                repo_root=repo_root,
            ),
            status,
            _duration_ms(testcase),
            message,
        )

    # 别名不能盖住任何真实用例
    aliases = {alias: primary for alias, primary in aliases.items() if alias not in cases}
    return ParsedReport(
        cases=cases,
        source=ReportSource.JUNIT_XML_SALVAGED if truncated else ReportSource.JUNIT_XML,
        aliases=aliases,
        collection_errors=tuple(collection_errors),
        truncated=truncated,
        problem=problem,
        skipped_without_id=0,
        # XML 一定分不出非 strict 的 XPASS；有没有文本能补，由 parse_pytest_report 决定
        xpass_may_read_as_passed=True,
    )


def _split_leading_test_id(text: str) -> tuple[str, str]:
    """从行首切出用例 ID，返回 (ID, 剩下的部分)。

    不能按空格粗暴切：参数化用例的 ID 里可能有空格
    （`test_param[带空格 的]`）。这里数方括号深度，只在深度为 0 时才认空格。
    """
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        elif char.isspace() and depth == 0:
            return text[:index], text[index:].lstrip()
    return text, ""


def _strip_message_dash(rest: str) -> str | None:
    """短摘要里 ID 后面跟的是 `- 原因`，把前面那个横杠去掉。"""
    text = rest.strip()
    if text.startswith("- "):
        text = text[2:].strip()
    return text[:MAX_MESSAGE_EXCERPT] or None


def parse_pytest_text(text: str, *, repo_root: Path | str | None = None) -> ParsedReport:
    """从 pytest 的文本输出里捞结果。junitxml 没生成时的兜底。

    先扫 `-v` 的逐条行（信息最全，SKIPPED 也带 ID），再用 `-rA` 的短摘要
    补上漏掉的用例和失败原因。短摘要只在 `short test summary info` 那一节里认，
    免得把被测代码自己打印的同形状文本当成测试结果。
    """
    cases: dict[str, TestCaseResult] = {}
    collection_errors: list[CollectionError] = []
    skipped_without_id = 0
    in_summary = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _SUMMARY_HEADER in line:
            in_summary = True
            continue

        test_id, tail = _split_leading_test_id(line)
        tail_match = _VERBOSE_TAIL.match(tail)
        if tail_match and "::" in test_id:
            normalized = normalize_test_id(test_id, repo_root=repo_root)
            reason = tail_match.group(2)
            cases.setdefault(
                normalized,
                TestCaseResult(
                    test_id=normalized,
                    status=_TEXT_STATUSES[tail_match.group(1)],
                    duration_ms=None,
                    message_excerpt=reason.strip("()")[:MAX_MESSAGE_EXCERPT] if reason else None,
                ),
            )
            continue

        summary = _SUMMARY_LINE.match(line) if in_summary else None
        if summary is None:
            continue
        status, rest = _TEXT_STATUSES[summary.group(1)], summary.group(2)
        if status is TestStatus.SKIPPED and _SKIPPED_COUNT_PREFIX.match(rest):
            # `SKIPPED [1] tests/test_a.py:26: reason` —— 只有文件:行号，没有用例 ID。
            # 猜一个出来就是在编数据，宁可少一条也不猜。
            skipped_without_id += 1
            continue
        summary_id, remainder = _split_leading_test_id(rest)
        if "::" not in summary_id:
            if status is TestStatus.ERROR:
                # `ERROR tests/test_broken.py` —— 整个模块收集失败
                collection_errors.append(
                    CollectionError(
                        module_path=normalize_test_id(summary_id, repo_root=repo_root),
                        message_excerpt=_strip_message_dash(remainder),
                    )
                )
            continue
        normalized = normalize_test_id(summary_id, repo_root=repo_root)
        message = _strip_message_dash(remainder)
        status = _with_strict_xpass_marker(status, message)
        existing = cases.get(normalized)
        if existing is None:
            cases[normalized] = TestCaseResult(normalized, status, None, message)
        elif existing.status is TestStatus.FAILED and status is TestStatus.XPASS:
            # 逐条行只打了 FAILED，`[XPASS(strict)]` 标记只出现在短摘要行里
            cases[normalized] = replace(existing, status=status, message_excerpt=message)
        elif existing.message_excerpt is None and message is not None:
            cases[normalized] = replace(existing, message_excerpt=message)

    return ParsedReport(
        cases=cases,
        source=ReportSource.STDOUT if cases or collection_errors else ReportSource.NONE,
        aliases={},
        collection_errors=tuple(collection_errors),
        truncated=False,
        problem=None if cases or collection_errors else "文本输出里没有可解析的用例结果",
        skipped_without_id=skipped_without_id,
        # 文本输出把 XPASS 打印成 `XPASS`，认得出来
        xpass_may_read_as_passed=False,
    )


def _apply_text_xpass(xml_report: ParsedReport, text_report: ParsedReport) -> ParsedReport:
    """用文本输出把 XML 里认不出的非 strict XPASS 补回来。

    **只做 `PASSED` → `XPASS` 这一种升级。** XML 是权威，文本只填它表达不了的那个洞；
    让文本推翻 XML 的其他判断，等于给判定引入第二个真相来源，那正是不确定性的来源。
    """
    if not text_report.cases:
        return xml_report
    upgraded = dict(xml_report.cases)
    for test_id, case in xml_report.cases.items():
        if case.status is not TestStatus.PASSED:
            continue
        from_text = text_report.resolve(test_id)
        if from_text is not None and from_text.status is TestStatus.XPASS:
            upgraded[test_id] = replace(
                case, status=TestStatus.XPASS, message_excerpt=from_text.message_excerpt
            )
    return replace(xml_report, cases=upgraded, xpass_may_read_as_passed=False)


def parse_pytest_report(
    report_path: Path | None,
    stdout: str = "",
    stderr: str = "",
    *,
    repo_root: Path | str | None = None,
) -> ParsedReport:
    """解析一次测试运行的产物。junitxml 优先，没有就从文本兜底。

    `repo_root` 是测试在容器里的工作目录（比如 `/workspace`）。给了它，报告里的
    绝对路径才能切回仓库相对路径。不给也能跑 —— `ParsedReport.resolve()` 还有一层
    按路径后缀兜底 —— 但那一层要求全局唯一，能给就给。
    """
    text_report = parse_pytest_text(stdout + ("\n" + stderr if stderr else ""), repo_root=repo_root)

    if report_path is None or not report_path.exists():
        return replace(
            text_report,
            problem=text_report.problem or f"没有测试报告文件：{report_path}",
        )
    raw = report_path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return replace(text_report, problem="测试报告文件是空的")

    xml_report = parse_junit_xml(raw, repo_root=repo_root)
    if xml_report.source is ReportSource.NONE:
        # XML 一条都没救回来，退回文本；两边的问题描述都留着，复核时要看
        problems = [p for p in (xml_report.problem, text_report.problem) if p]
        return replace(text_report, problem="；".join(problems) or None)
    return _apply_text_xpass(xml_report, text_report)


class PytestReportParser:
    """pytest 报告解析器（`TestReportParser` 的实现）。

    `repo_root` 传测试在容器里的工作目录。整个类没有可变状态，
    同一个实例反复用、并发用都行。
    """

    def __init__(self, *, repo_root: Path | str | None = None) -> None:
        self.repo_root = repo_root

    def parse(self, report_path: Path | None, stdout: str = "", stderr: str = "") -> ParsedReport:
        return parse_pytest_report(report_path, stdout, stderr, repo_root=self.repo_root)


__all__ = [
    "MAX_MESSAGE_EXCERPT",
    "CollectionError",
    "IntegrityCheck",
    "ParsedReport",
    "PytestReportParser",
    "ReportSource",
    "TestCaseResult",
    "TestReportParser",
    "parse_junit_xml",
    "parse_pytest_report",
    "parse_pytest_text",
]
