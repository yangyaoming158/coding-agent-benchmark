"""测试报告解析器的单测（E4-T1，AC 第 1 条）。

对着 `tests/fixtures/reports/` 里 12 份**真实录制**的报告跑。那些 fixture 由
`tests/fixtures/reports/_record.py` 用真的 pytest 生成，不是手写的 XML——
手写的只会包含"我以为 pytest 会输出什么"，漏掉的怪癖恰恰是出静默 bug 的地方。

断言的是不变量，不是"当前跑出来是什么"：
每条断言都能追到 `_record.py` 里那段源码的**语义**（这个函数写的是断言失败，
所以它必须被报成 FAILED），而不是某次运行的快照。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 起别名不是讲究：直接 `import TestStatus` 会被 pytest 当成待收集的测试类，
# 每跑一次多一条 PytestCollectionWarning。
from app.domain.enums import TestStatus as Status
from app.judge.report_parser import (
    MAX_MESSAGE_EXCERPT,
    ParsedReport,
    PytestReportParser,
    ReportSource,
    parse_junit_xml,
    parse_pytest_report,
    parse_pytest_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "reports"
GOLDEN_TASK = (
    Path(__file__).resolve().parents[3] / "datasets" / "golden" / "bench-golden__textkit-1.json"
)

#: 全部 12 份 fixture。新增 fixture 时这里也要加 —— 有几条通用不变量对每一份都跑。
ALL_FIXTURES = (
    "shapes_xunit2.xml",
    "shapes_xunit1.xml",
    "shapes_stdout.txt",
    "shapes_quiet_stdout.txt",
    "collection_error_xunit2.xml",
    "collection_error_stdout.txt",
    "empty_xunit2.xml",
    "truncated_xunit2.xml",
    "golden_textkit_base_xunit2.xml",
    "golden_textkit_base_stdout.txt",
    "golden_textkit_fixed_xunit2.xml",
    "golden_textkit_fixed_xunit1.xml",
)

#: `_record.py` 里 `SHAPES_SOURCES` 每个函数**按语义**应该被报成什么。
#: 左边这一列就是那份源码里的用例，右边是它写出来的行为决定的状态。
SHAPES_FROM_XML = {
    "tests/sub/test_nested.py::test_deep": Status.PASSED,
    "tests/test_shapes.py::test_ok": Status.PASSED,
    # assert 1 == 2
    "tests/test_shapes.py::test_assert_fail": Status.FAILED,
    # 函数体里 raise RuntimeError —— junitxml 记成 <failure>，所以是 FAILED 不是 ERROR
    "tests/test_shapes.py::test_raises_runtime": Status.FAILED,
    # fixture 里抛异常 —— 这才是 <error>
    "tests/test_shapes.py::test_setup_error": Status.ERROR,
    "tests/test_shapes.py::test_skipped": Status.SKIPPED,
    "tests/test_shapes.py::test_xfail": Status.XFAIL,
    # 非 strict 的 xfail 通过了。junitxml 表达不了 XPASS，只能报 PASSED
    "tests/test_shapes.py::test_xpass": Status.PASSED,
    # strict 的 xfail 通过了 —— 这种带 [XPASS(strict)] 标记，认得出来
    "tests/test_shapes.py::test_xpass_strict": Status.XPASS,
    "tests/test_shapes.py::test_param[1]": Status.PASSED,
    "tests/test_shapes.py::test_param[2]": Status.PASSED,
    # 非 ASCII 参数在 XML 里是字面的 \uXXXX，必须还原成中文才对得上
    "tests/test_shapes.py::test_param[带空格 的]": Status.PASSED,
    "tests/test_shapes.py::TestGroup::test_method": Status.PASSED,
    "tests/test_shapes.py::TestGroup::test_method_param[0]": Status.PASSED,
    "tests/test_shapes.py::TestGroup::test_method_param[1]": Status.PASSED,
}

#: 有文本输出时，唯一的差别是那条非 strict 的 XPASS 能被认出来。
SHAPES_WITH_TEXT = {**SHAPES_FROM_XML, "tests/test_shapes.py::test_xpass": Status.XPASS}


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse_fixture(name: str) -> ParsedReport:
    """按后缀选解析入口：`.xml` 走 junitxml，`.txt` 走文本兜底。"""
    if name.endswith(".xml"):
        return parse_junit_xml(read(name))
    return parse_pytest_text(read(name))


# ── 对每一份 fixture 都成立的不变量 ─────────────────────────


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses(name: str) -> None:
    """12 份 fixture 全部能解析，不抛异常。"""
    assert isinstance(parse_fixture(name), ParsedReport)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parsing_is_deterministic(name: str) -> None:
    """同一份报告解析两次，逐字段必须相同（`AGENTS.md` §5.1）。

    判定有随机性的话，这个月的排行榜和下个月的排行榜就没法放在一起看。
    解析器是判定链上最靠前的一环，它抖一下，后面全抖。
    """
    first, second = parse_fixture(name), parse_fixture(name)
    assert first == second


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parser_never_emits_missing(name: str) -> None:
    """解析器不产生 MISSING。

    MISSING 的定义是"题目里列了、报告里找不到"（协议 C-11），它是**比对**的结果，
    只有判定引擎手里同时有题目和报告时才判得出来。解析器只报它看见的东西。
    """
    assert all(c.status is not Status.MISSING for c in parse_fixture(name).cases.values())


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_message_excerpt_fits_the_column(name: str) -> None:
    """摘要不能超过 `test_results.message_excerpt` 的列宽，否则入库时才炸。"""
    for case in parse_fixture(name).cases.values():
        assert case.message_excerpt is None or len(case.message_excerpt) <= MAX_MESSAGE_EXCERPT


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_case_id_matches_dict_key(name: str) -> None:
    """键和 `TestCaseResult.test_id` 必须一致，不然按键查和按值查会给出两个答案。"""
    for test_id, case in parse_fixture(name).cases.items():
        assert case.test_id == test_id


# ── 十一种用例形态 ──────────────────────────────────────────


def test_shapes_xunit2_statuses() -> None:
    """默认的 xunit2 family：没有 `file` 属性，全靠 classname 切分。"""
    assert parse_junit_xml(read("shapes_xunit2.xml")).statuses == SHAPES_FROM_XML


def test_shapes_xunit1_statuses() -> None:
    """xunit1 family：有 `file` 属性，路径直接照抄。"""
    assert parse_junit_xml(read("shapes_xunit1.xml")).statuses == SHAPES_FROM_XML


def test_both_junit_families_agree() -> None:
    """同一次运行的两种 family 必须解析出完全相同的结果。

    这条是 classname 切分逻辑的判卷标准：xunit1 有 `file` 不用猜，xunit2 得猜。
    两边对不上就说明猜错了。
    """
    assert (
        parse_junit_xml(read("shapes_xunit2.xml")).statuses
        == parse_junit_xml(read("shapes_xunit1.xml")).statuses
    )


def test_xunit1_has_no_alternate_ids() -> None:
    """有 `file` 属性时没有歧义，不该留备选 ID。"""
    assert parse_junit_xml(read("shapes_xunit1.xml")).aliases == {}
    assert parse_junit_xml(read("shapes_xunit2.xml")).aliases != {}


def test_error_versus_failure_is_not_by_intuition() -> None:
    """函数体里抛异常是 FAILED，fixture 里抛异常才是 ERROR。

    单独拎出来断言，是因为这一条特别容易按直觉写反，而写反之后
    `06-judge-attribution.md` §12 的失败归因会把"测试代码坏了"和"被测代码坏了"搞混。
    """
    cases = parse_junit_xml(read("shapes_xunit2.xml")).cases
    assert cases["tests/test_shapes.py::test_raises_runtime"].status is Status.FAILED
    assert cases["tests/test_shapes.py::test_setup_error"].status is Status.ERROR


def test_skipped_and_xfail_are_never_passed() -> None:
    """协议 C-12：禁止把 MISSING / SKIPPED / XFAIL 当作通过。"""
    cases = parse_junit_xml(read("shapes_xunit2.xml")).cases
    assert cases["tests/test_shapes.py::test_skipped"].status is Status.SKIPPED
    assert cases["tests/test_shapes.py::test_xfail"].status is Status.XFAIL


def test_durations_are_parsed() -> None:
    """`time` 属性要变成毫秒。"""
    cases = parse_junit_xml(read("shapes_xunit2.xml")).cases
    assert cases["tests/test_shapes.py::test_ok"].duration_ms == 10


def test_failure_message_is_captured() -> None:
    excerpt = (
        parse_junit_xml(read("shapes_xunit2.xml"))
        .cases["tests/test_shapes.py::test_assert_fail"]
        .message_excerpt
    )
    assert excerpt is not None
    assert "one is not two" in excerpt


# ── XPASS：junitxml 表达不了，不许装作能分出来 ─────────────


def test_non_strict_xpass_reads_as_passed_from_xml_alone() -> None:
    """只有 XML 时，非 strict 的 XPASS 会被如实报成 PASSED，并把盲区标出来。"""
    report = parse_junit_xml(read("shapes_xunit2.xml"))
    assert report.cases["tests/test_shapes.py::test_xpass"].status is Status.PASSED
    assert report.xpass_may_read_as_passed is True


def test_text_output_recovers_non_strict_xpass() -> None:
    """两边都有时，用文本把 XML 认不出的那条 XPASS 补回来。"""
    report = parse_pytest_report(FIXTURES / "shapes_xunit2.xml", read("shapes_stdout.txt"))
    assert report.statuses == SHAPES_WITH_TEXT
    assert report.xpass_may_read_as_passed is False


def test_text_only_upgrades_passed_to_xpass() -> None:
    """文本只填 XML 表达不了的那个洞，不许推翻 XML 的其他判断。

    构造一份"文本说全部通过"的输入喂进去，XML 里的失败必须原封不动。
    """
    faked = "\n".join(f"{test_id} PASSED                     [ 50%]" for test_id in SHAPES_FROM_XML)
    report = parse_pytest_report(FIXTURES / "shapes_xunit2.xml", faked)
    assert report.cases["tests/test_shapes.py::test_assert_fail"].status is Status.FAILED
    assert report.cases["tests/test_shapes.py::test_skipped"].status is Status.SKIPPED


# ── 文本兜底 ────────────────────────────────────────────────


def test_text_fallback_covers_all_shapes() -> None:
    """`-v -rA` 的文本输出能捞到全部用例，而且比 XML 多认出一条 XPASS。"""
    report = parse_pytest_text(read("shapes_stdout.txt"))
    assert report.source is ReportSource.STDOUT
    assert report.statuses == SHAPES_WITH_TEXT


def test_text_fallback_counts_skipped_without_id() -> None:
    """短摘要里的 SKIPPED 行只有"文件:行号"，拿不到用例 ID —— 只计数，不猜。

    逐条行（`-v`）那边能拿到 ID，所以这条用例本身没丢；计数是为了让上层知道
    "这份文本里有一条 SKIPPED 是从摘要行认不出来的"。
    """
    assert parse_pytest_text(read("shapes_stdout.txt")).skipped_without_id == 1


def test_default_quiet_output_only_recovers_failures() -> None:
    """默认输出没有 `-rA`，短摘要里只有失败和错误，通过的用例整批看不见。

    解析器如实报它捞到的那几条，**不给通过的用例编状态**。
    """
    report = parse_pytest_text(read("shapes_quiet_stdout.txt"))
    assert set(report.statuses) == {
        "tests/test_shapes.py::test_assert_fail",
        "tests/test_shapes.py::test_raises_runtime",
        "tests/test_shapes.py::test_xpass_strict",
        "tests/test_shapes.py::test_setup_error",
    }
    assert all(s is not Status.PASSED for s in report.statuses.values())


def test_text_fallback_is_never_called_complete() -> None:
    """文本兜底一律算"报告不完整"（C-13b 第 1 项）。

    因为"这条用例没出现"既可能是它没跑，也可能是它通过了但没被打印出来。
    分不出来的时候记 MISSING 再罚 AI，罚的其实是我们自己的 test_command 少写了参数。
    """
    check = parse_pytest_text(read("shapes_stdout.txt")).check_integrity([])
    assert check.report_complete is False
    assert check.report_problem is not None


def test_text_parser_ignores_traceback_noise() -> None:
    """traceback 和被测代码打印的东西不能被当成测试结果。"""
    noise = "\n".join(
        [
            ">       assert tests/test_a.py::test_x PASSED",
            "E       AssertionError",
            "PASSED tests/test_a.py::test_not_in_summary_section",
            "tests/test_a.py::test_real PASSED                    [100%]",
        ]
    )
    # 第 3 行不在 `short test summary info` 那一节里，不认
    assert set(parse_pytest_text(noise).statuses) == {"tests/test_a.py::test_real"}


# ── 收集失败 ────────────────────────────────────────────────


def test_collection_error_from_xml() -> None:
    """收集失败的条目 classname 是空的、name 是点分模块名，要还原成文件路径。"""
    report = parse_junit_xml(read("collection_error_xunit2.xml"))
    assert report.cases == {}
    assert [e.module_path for e in report.collection_errors] == ["brk/test_broken.py"]
    assert "ModuleNotFoundError" in (report.collection_errors[0].message_excerpt or "")


def test_collection_error_from_text() -> None:
    """短摘要里的 `ERROR <文件路径>`（没有 `::`）也是收集失败。"""
    report = parse_pytest_text(read("collection_error_stdout.txt"))
    assert [e.module_path for e in report.collection_errors] == ["brk/test_broken.py"]


def test_collection_error_alone_does_not_blame_harness() -> None:
    """收集错误既可能是 AI 改坏了 import，也可能是题目坏了 —— 解析器分不出来。

    所以它只如实记录，不把锅扣给平台。分支交给 E4-T3 按 C-13c 的实际证据判。
    """
    check = parse_junit_xml(read("collection_error_xunit2.xml")).check_integrity([])
    assert check.collection_error_modules == ("brk/test_broken.py",)
    assert check.report_complete is True
    assert check.blames_harness is False


# ── 空报告与截断的报告 ──────────────────────────────────────


def test_empty_report_is_complete_but_has_no_cases() -> None:
    """一条用例都没收集到 ≠ 报告坏了。空报告本身是完整的。"""
    report = parse_junit_xml(read("empty_xunit2.xml"))
    assert report.cases == {}
    assert report.truncated is False
    assert report.check_integrity([]).report_complete is True


def test_truncated_report_salvages_the_first_half() -> None:
    """容器被杀导致 XML 写了一半时，救回前半截，并标明报告不完整。

    整份丢掉的话，"10 条里有 5 条通过了"和"一条都没跑"就分不出来了，
    而这两种在归因上完全不同。
    """
    report = parse_junit_xml(read("truncated_xunit2.xml"))
    full = parse_junit_xml(read("shapes_xunit2.xml"))
    assert report.source is ReportSource.JUNIT_XML_SALVAGED
    assert report.truncated is True
    assert 0 < len(report.cases) < len(full.cases)
    # 救回来的部分必须和完整报告逐条一致，不能是猜的
    assert all(full.cases[test_id] == case for test_id, case in report.cases.items())


def test_truncated_report_blames_harness() -> None:
    """报告被截断是平台自己的问题，按 C-13 的 (a) 分支走，不罚 AI。"""
    report = parse_junit_xml(read("truncated_xunit2.xml"))
    check = report.check_integrity(["tests/test_shapes.py::TestGroup::test_method"])
    assert check.report_complete is False
    assert check.blames_harness is True
    assert check.missing_ids == ("tests/test_shapes.py::TestGroup::test_method",)


def test_unparseable_xml_falls_back_to_text() -> None:
    """XML 一条都救不回来时退回文本，两边的问题描述都留着。"""
    broken = FIXTURES / "empty_xunit2.xml"
    report = parse_pytest_report(broken, read("shapes_stdout.txt"))
    # 空报告是能解析的，所以走的是 XML 分支
    assert report.source is ReportSource.JUNIT_XML

    garbage = parse_junit_xml("<<< 这不是 XML")
    assert garbage.source is ReportSource.NONE
    assert garbage.problem is not None


def test_missing_report_file_falls_back_to_text() -> None:
    report = parse_pytest_report(FIXTURES / "does_not_exist.xml", read("shapes_stdout.txt"))
    assert report.source is ReportSource.STDOUT
    assert report.statuses == SHAPES_WITH_TEXT
    assert report.problem is not None


def test_no_report_and_no_text_gives_nothing() -> None:
    report = parse_pytest_report(None, "", "")
    assert report.cases == {}
    assert report.source is ReportSource.NONE
    assert report.check_integrity([]).report_complete is False


# ── 真实 Golden 题的报告 ────────────────────────────────────


def golden_task() -> dict:
    return json.loads(GOLDEN_TASK.read_text(encoding="utf-8"))


def test_golden_base_report_matches_the_task_definition() -> None:
    """真实题目在 base 上跑：每条 F2P 必须失败，每条 P2P 必须通过。

    这正是六步验证第 3、4 步的结论，也是 Noop 哨兵解决率为 0% 的依据。
    用题目 JSON 里的 ID 去 `resolve()`，等于把"题目写的 ID"和"报告里的 ID"
    对了一遍 —— 归一化写错的话这条就红。
    """
    task = golden_task()
    report = parse_pytest_report(
        FIXTURES / "golden_textkit_base_xunit2.xml", read("golden_textkit_base_stdout.txt")
    )
    for test_id in task["fail_to_pass"]:
        case = report.resolve(test_id)
        assert case is not None, f"F2P 用例在报告里找不到：{test_id}"
        assert case.status is Status.FAILED
    for test_id in task["pass_to_pass"]:
        case = report.resolve(test_id)
        assert case is not None, f"P2P 用例在报告里找不到：{test_id}"
        assert case.status is Status.PASSED


def test_golden_fixed_report_has_everything_passing() -> None:
    """同一道题打上 gold_patch 之后：F2P 和 P2P 全过 —— Oracle 哨兵 100% 的依据。"""
    task = golden_task()
    report = parse_junit_xml(read("golden_textkit_fixed_xunit2.xml"))
    for test_id in [*task["fail_to_pass"], *task["pass_to_pass"]]:
        case = report.resolve(test_id)
        assert case is not None, test_id
        assert case.status is Status.PASSED
    assert report.check_integrity(task["fail_to_pass"]).missing_ids == ()


def test_golden_both_families_agree() -> None:
    assert (
        parse_junit_xml(read("golden_textkit_fixed_xunit2.xml")).statuses
        == parse_junit_xml(read("golden_textkit_fixed_xunit1.xml")).statuses
    )


# ── resolve()：防假 MISSING 的最后一道 ──────────────────────


def test_resolve_matches_across_id_shapes() -> None:
    """题目里的 ID 写成什么形状都要能对上报告里的用例。"""
    report = parse_junit_xml(read("shapes_xunit2.xml"))
    for raw in (
        "tests/test_a.py::test_x".replace("test_a", "test_shapes").replace("test_x", "test_ok"),
        "./tests/test_shapes.py::test_ok",
        r"tests\test_shapes.py::test_ok",
        "tests//test_shapes.py::test_ok",
    ):
        case = report.resolve(raw)
        assert case is not None, raw
        assert case.test_id == "tests/test_shapes.py::test_ok"


def test_resolve_falls_back_to_path_suffix() -> None:
    """报告里是绝对路径、又没给 `repo_root` 时，靠路径后缀兜住。"""
    xml = (
        '<testsuites><testsuite><testcase classname="home.u.repo.tests.test_a" '
        'name="test_x" time="0.01"/></testsuite></testsuites>'
    )
    report = parse_junit_xml(xml)
    assert report.resolve("tests/test_a.py::test_x") is not None


def test_resolve_refuses_ambiguous_suffix_match() -> None:
    """两条用例的路径都以它结尾时返回 None。

    猜错一条的后果是把 A 的结果安到 B 头上，那比记一条 MISSING 严重得多。
    """
    xml = (
        "<testsuites><testsuite>"
        '<testcase classname="a.tests.test_a" name="test_x" time="0.01"/>'
        '<testcase classname="b.tests.test_a" name="test_x" time="0.01"/>'
        "</testsuite></testsuites>"
    )
    assert parse_junit_xml(xml).resolve("tests/test_a.py::test_x") is None


def test_resolve_uses_alternate_ids_when_classname_is_ambiguous() -> None:
    """classname 切错时，备选 ID 能把用例救回来。

    `pkg.helpers.Checks` 里 `Checks` 不以 `Test` 开头，按约定打分会被当成模块的一段；
    题目里写的却是 `pkg/helpers.py::Checks::test_x`。备选 ID 就是为这种情况留的。
    """
    xml = (
        '<testsuites><testsuite><testcase classname="pkg.helpers.Checks" '
        'name="test_x" time="0.01"/></testsuite></testsuites>'
    )
    report = parse_junit_xml(xml)
    assert report.resolve("pkg/helpers.py::Checks::test_x") is not None
    assert report.resolve("pkg/helpers/Checks.py::test_x") is not None


def test_resolve_with_repo_root_strips_container_path() -> None:
    """容器里的工作目录是 `/workspace`，报告里的绝对路径要能切回相对路径。"""
    xml = (
        '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x" '
        'file="/workspace/tests/test_a.py" time="0.01"/></testsuite></testsuites>'
    )
    report = parse_junit_xml(xml, repo_root="/workspace")
    assert "tests/test_a.py::test_x" in report.cases


# ── C-13b 三项自检 ──────────────────────────────────────────


def test_integrity_check_reports_all_three_items() -> None:
    """三项自检都要交出来：报告完整性、ID 对照、收集错误。"""
    report = parse_junit_xml(read("shapes_xunit2.xml"))
    check = report.check_integrity(["tests/test_shapes.py::test_ok", "tests/test_gone.py::test_x"])
    assert check.report_complete is True
    assert check.missing_ids == ("tests/test_gone.py::test_x",)
    # 第 2 项要求把两边的 ID 都打印出来对照
    assert "tests/test_shapes.py::test_ok" in check.reported_ids
    assert check.collection_error_modules == ()


def test_integrity_check_points_at_normalization_mismatch() -> None:
    """节点部分一样、只有路径不同 —— 这就是归一化没对上的典型症状。

    与其让人拿两列 ID 肉眼比对，不如直接把嫌疑对指出来（C-13b 第 2 项）。
    """
    report = parse_junit_xml(read("shapes_xunit2.xml"))
    check = report.check_integrity(["src/test_shapes.py::test_ok"])
    assert check.near_misses == {"src/test_shapes.py::test_ok": "tests/test_shapes.py::test_ok"}
    assert check.blames_harness is True


def test_integrity_check_does_not_blame_harness_for_a_real_miss() -> None:
    """报告完整、也找不到长得像的 —— 这条 MISSING 不能算到平台头上。

    那时该走 C-13 的 (b) / (c) 分支，由 E4-T3 拿补丁证据去分。
    """
    check = parse_junit_xml(read("shapes_xunit2.xml")).check_integrity(
        ["tests/test_shapes.py::test_never_existed"]
    )
    assert check.missing_ids == ("tests/test_shapes.py::test_never_existed",)
    assert check.near_misses == {}
    assert check.blames_harness is False


def test_integrity_check_is_deterministic() -> None:
    """同一份报告、同一批期望 ID，两次自检结果必须相同。"""
    report = parse_junit_xml(read("shapes_xunit2.xml"))
    expected = ["src/test_shapes.py::test_ok", "tests/test_shapes.py::test_ok"]
    assert report.check_integrity(expected) == report.check_integrity(expected)


# ── Protocol 实现 ───────────────────────────────────────────


def test_parser_class_matches_the_functional_entry_point() -> None:
    """`PytestReportParser` 只是函数的一层壳，结果必须一致。"""
    parser = PytestReportParser()
    assert parser.parse(FIXTURES / "shapes_xunit2.xml", read("shapes_stdout.txt")) == (
        parse_pytest_report(FIXTURES / "shapes_xunit2.xml", read("shapes_stdout.txt"))
    )


def test_statuses_property_is_the_documented_shape() -> None:
    """§11.3 里 `TestReportParser.parse` 写的返回值形状是 `dict[str, TestStatus]`。"""
    statuses = parse_junit_xml(read("shapes_xunit2.xml")).statuses
    assert all(isinstance(v, Status) for v in statuses.values())
