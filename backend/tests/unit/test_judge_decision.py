"""判定引擎的单测（E4-T3）。

AC 有两条：**真值表单测全过**、**同补丁重判 3 次结果与逐用例状态完全一致**。

真值表这里是**穷举**的：`InfraOutcome`（13 个）× "AI 启没启动"（2 种）全跑一遍，
每一格要么产出协议 §4.3 认可的合法组合，要么因为输入自相矛盾而拒绝。
穷举比挑几个典型值靠谱得多 —— 协议 v1.2 里 C-18 的第 8 组矛盾就是穷举跑出来的，
人工逐条看两遍都没发现。
"""

from __future__ import annotations

import pytest

# TestRole / TestStatus / TestCaseResult 都起了别名：直接 import 会被 pytest 当成
# 待收集的测试类，每跑一次多一条 PytestCollectionWarning。
from app.domain.enums import AgentOutcome, InfraOutcome, LifecycleStatus
from app.domain.enums import TestRole as Role
from app.domain.enums import TestStatus as Status
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    IllegalCombinationError,
    InfraFailureCounting,
    is_legal_combination,
)
from app.judge.decision import (
    AgentFacts,
    ControlRunRequiredError,
    ReviewFlag,
    judge,
)
from app.judge.report_parser import CollectionError, ParsedReport, ReportSource
from app.judge.report_parser import TestCaseResult as CaseResult

F2P = ("tests/test_a.py::test_new_feature",)
P2P = ("tests/test_a.py::test_old_feature",)


def make_report(
    statuses: dict[str, Status],
    *,
    source: ReportSource = ReportSource.JUNIT_XML,
    truncated: bool = False,
    problem: str | None = None,
    collection_errors: tuple[CollectionError, ...] = (),
) -> ParsedReport:
    """按"用例 ID → 状态"造一份报告。

    直接构造而不是走解析：这一层测的是判定逻辑，从 XML 出发只会把解析器的行为
    也拖进断言里，两边任何一个改了都会让这组用例红。
    """
    return ParsedReport(
        cases={
            test_id: CaseResult(test_id, status, 10, None) for test_id, status in statuses.items()
        },
        source=source,
        aliases={},
        collection_errors=collection_errors,
        truncated=truncated,
        problem=problem,
        skipped_without_id=0,
        xpass_may_read_as_passed=False,
    )


def all_passing() -> ParsedReport:
    return make_report({F2P[0]: Status.PASSED, P2P[0]: Status.PASSED})


# ── 真值表：穷举 infra_outcome × 启没启动 ───────────────────


@pytest.mark.parametrize("infra_outcome", list(InfraOutcome))
@pytest.mark.parametrize("agent_started", [True, False])
def test_every_infra_outcome_yields_a_legal_combination(
    infra_outcome: InfraOutcome, agent_started: bool
) -> None:
    """13 × 2 全跑一遍：要么合法，要么因为输入自相矛盾而拒绝。

    绝不允许的是第三种情况 —— 产出一个协议 §4.3 里没有的组合还静默落库（C-78）。
    """
    kwargs = {
        "infra_outcome": infra_outcome,
        "report": all_passing(),
        "fail_to_pass": F2P,
        "pass_to_pass": P2P,
        "facts": AgentFacts(agent_started=agent_started),
    }
    if infra_outcome is InfraOutcome.TEST_TIMEOUT:
        kwargs["control_run_timed_out"] = False

    try:
        verdict = judge(**kwargs)  # type: ignore[arg-type]
    except IllegalCombinationError:
        # 例如 WORKSPACE_ERROR + agent_started=True：物化失败必然发生在 AI 启动之前，
        # 这样的输入本身就是上游的 bug，拒绝是对的（C-69、C-77）
        return

    assert is_legal_combination(
        verdict.lifecycle_status,
        verdict.infra_outcome,
        verdict.agent_outcome,
        agent_started,
    ), f"{infra_outcome} + started={agent_started} 产出了非法组合"


@pytest.mark.parametrize("infra_outcome", list(InfraOutcome))
def test_infra_failure_counting_matches_the_mapping_table(infra_outcome: InfraOutcome) -> None:
    """ "算不算平台故障"必须和 C-18 映射表一字不差。

    这个数字是平台故障率的分子，而平台故障率超过 5% 整个实验就不进排行榜（C-26）。
    判定引擎自己拍一个值，等于绕过了那道闸。
    """
    rule = INFRA_TO_AGENT_MAPPING[infra_outcome]
    if rule.counts_as_infra_failure is InfraFailureCounting.BY_CONTROL_RUN:
        # TEST_TIMEOUT 要看对照组：对照组也超时才算平台故障（C-20 第 4、5 步）
        for control_timed_out in (True, False):
            verdict = judge(
                infra_outcome=infra_outcome,
                report=all_passing(),
                fail_to_pass=F2P,
                control_run_timed_out=control_timed_out,
            )
            assert verdict.counts_as_infra_failure is control_timed_out
        return

    started = infra_outcome not in {
        InfraOutcome.ENV_BUILD_FAILED,
        InfraOutcome.WORKSPACE_ERROR,
    }
    try:
        verdict = judge(
            infra_outcome=infra_outcome,
            report=all_passing(),
            fail_to_pass=F2P,
            pass_to_pass=P2P,
            facts=AgentFacts(agent_started=started),
        )
    except IllegalCombinationError:
        return
    assert verdict.counts_as_infra_failure is (
        rule.counts_as_infra_failure is InfraFailureCounting.YES
    )


def test_agent_faults_are_completed_not_failed() -> None:
    """责任在 AI 的故障要判 `COMPLETED`，不是 `FAILED`。

    这条最容易写反 —— 直觉觉得"出错了就是 FAILED"。可那样一来，AI 只要把自己
    搞崩（超时、崩溃、交个打不上的补丁）就能从解决率的分母里消失，
    越不稳定的 AI 分数越好看。
    """
    for infra_outcome, expected in (
        (InfraOutcome.AGENT_TIMEOUT, AgentOutcome.UNRESOLVED),
        (InfraOutcome.AGENT_RUNTIME_ERROR, AgentOutcome.UNRESOLVED),
        (InfraOutcome.PATCH_APPLY_FAILED, AgentOutcome.INVALID_PATCH),
    ):
        verdict = judge(infra_outcome=infra_outcome, report=None, fail_to_pass=F2P)
        assert verdict.lifecycle_status is LifecycleStatus.COMPLETED, infra_outcome
        assert verdict.agent_outcome is expected
        assert verdict.counts_as_infra_failure is False


def test_cancelled_gets_its_own_terminal_state() -> None:
    """人工取消的终态是 `CANCELLED`，不是 `FAILED`（C-68 第六行）。"""
    verdict = judge(infra_outcome=InfraOutcome.CANCELLED, report=None, fail_to_pass=F2P)
    assert verdict.lifecycle_status is LifecycleStatus.CANCELLED
    assert verdict.agent_outcome is None
    assert verdict.counts_as_infra_failure is False


def test_not_attempted_requires_agent_never_started() -> None:
    """`NOT_ATTEMPTED` 当且仅当 AI 从未启动（C-69）。"""
    verdict = judge(
        infra_outcome=InfraOutcome.SANDBOX_ERROR,
        report=None,
        fail_to_pass=F2P,
        facts=AgentFacts(agent_started=False),
    )
    assert verdict.agent_outcome is AgentOutcome.NOT_ATTEMPTED

    verdict = judge(
        infra_outcome=InfraOutcome.SANDBOX_ERROR,
        report=None,
        fail_to_pass=F2P,
        facts=AgentFacts(agent_started=True),
    )
    assert verdict.agent_outcome is None, "AI 已经启动过了，就不能说它没被尝试"


# ── C-08：按逐条用例判 ──────────────────────────────────────


def test_all_passing_is_resolved() -> None:
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=all_passing(),
        fail_to_pass=F2P,
        pass_to_pass=P2P,
    )
    assert verdict.agent_outcome is AgentOutcome.RESOLVED
    assert verdict.f2p_ok and verdict.p2p_ok


def test_one_failing_f2p_is_unresolved() -> None:
    report = make_report({F2P[0]: Status.FAILED, P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert verdict.f2p_ok is False


def test_broken_p2p_is_unresolved_even_when_f2p_passes() -> None:
    """P2P 就是"别把别的功能改坏"的检查范围，挂了一条就不算修好。"""
    report = make_report({F2P[0]: Status.PASSED, P2P[0]: Status.FAILED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert verdict.f2p_ok is True
    assert verdict.p2p_ok is False


def test_empty_f2p_list_is_never_resolved() -> None:
    """一条 F2P 都没有的题不能判成修好。

    "空集的全称命题为真"会让一道坏题（忘了填 F2P）对所有 AI 都判 RESOLVED，
    而且不报任何错 —— 解决率会凭空变高。
    """
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=make_report({P2P[0]: Status.PASSED}),
        fail_to_pass=(),
        pass_to_pass=P2P,
    )
    assert verdict.agent_outcome is not AgentOutcome.RESOLVED


def test_empty_p2p_list_is_allowed() -> None:
    """P2P 可以为空 —— 题目可以不设回归检查范围。"""
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=make_report({F2P[0]: Status.PASSED}),
        fail_to_pass=F2P,
        pass_to_pass=(),
    )
    assert verdict.agent_outcome is AgentOutcome.RESOLVED


# ── C-08c：EMPTY_PATCH 和 UNRESOLVED 怎么分 ─────────────────


def test_empty_patch_after_normal_exit() -> None:
    """正常退出 + 空补丁 → `EMPTY_PATCH`。"""
    report = make_report({F2P[0]: Status.FAILED, P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=report,
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(normalized_patch_empty=True, exited_normally=True),
    )
    assert verdict.agent_outcome is AgentOutcome.EMPTY_PATCH


def test_empty_patch_after_abnormal_exit_is_unresolved() -> None:
    """异常终止 + 空补丁 → `UNRESOLVED`（C-08c）。

    超时被杀的 AI 可能正要写文件就没了，把它算成"跑完了但没交东西"，
    空补丁率这个诊断指标就废了。
    """
    report = make_report({F2P[0]: Status.FAILED, P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=report,
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(normalized_patch_empty=True, exited_normally=False),
    )
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED


def test_empty_patch_that_passes_everything_is_resolved() -> None:
    """空补丁却全过 → 判 `RESOLVED`，**不是** `EMPTY_PATCH`。

    这是 Noop 哨兵能发现坏题的前提：用空补丁跑整个数据集，解决率必须 0%，
    不是 0% 就说明有题目在修复前测试就已经通过了。

    判成 `EMPTY_PATCH` 的话，坏题会让哨兵显示 0%（看着完全正常），
    于是那道题永远不会被发现。
    """
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=all_passing(),
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(normalized_patch_empty=True, exited_normally=True),
    )
    assert verdict.agent_outcome is AgentOutcome.RESOLVED


# ── C-12：这些状态一律不算通过 ──────────────────────────────


@pytest.mark.parametrize(
    "status", [Status.SKIPPED, Status.XFAIL, Status.XPASS, Status.MISSING, Status.ERROR]
)
def test_non_passed_statuses_never_count_as_passing(status: Status) -> None:
    """协议 C-12：禁止把 MISSING / SKIPPED / XFAIL 当作通过。

    XPASS 和 ERROR 也一并挡掉：一条本该失败却通过了的用例说明题目或标记有问题，
    不能当作修好的证据。
    """
    report = make_report({F2P[0]: status, P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )
    assert verdict.agent_outcome is not AgentOutcome.RESOLVED
    assert verdict.f2p_ok is False


# ── C-13：出现 MISSING 时的三分支 ───────────────────────────


def test_missing_from_a_truncated_report_blames_the_platform() -> None:
    """(a) 报告被截断 → 判平台故障，**不罚 AI**（C-13a）。"""
    report = make_report(
        {P2P[0]: Status.PASSED},
        source=ReportSource.JUNIT_XML_SALVAGED,
        truncated=True,
        problem="junitxml 被截断",
    )
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )

    assert verdict.lifecycle_status is LifecycleStatus.FAILED
    assert verdict.infra_outcome is InfraOutcome.HARNESS_ERROR
    assert verdict.agent_outcome is None
    assert verdict.counts_as_infra_failure is True
    assert verdict.integrity is not None


def test_missing_with_a_near_miss_blames_the_platform() -> None:
    """(a) 报告里有一条长得几乎一样、只有路径不同的用例 → 归一化写错了，是我们的锅。

    这正是全项目最容易出的静默 bug：`tests/test_a.py::test_x` 和
    `./tests/test_a.py::test_x` 匹配不上，于是每道题都莫名其妙地失败。
    """
    report = make_report({"src/test_a.py::test_new_feature": Status.PASSED, P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )

    assert verdict.infra_outcome is InfraOutcome.HARNESS_ERROR
    assert verdict.agent_outcome is None
    assert verdict.integrity is not None and verdict.integrity.near_misses


def test_missing_with_a_collection_error_blames_the_agent() -> None:
    """(b) 收集阶段报错 → 有实际证据，判 AI 没修好。

    前提是受保护路径已经被强制还原过（E4-T2），官方测试是完好的 ——
    那 import 挂掉只能是 AI 改的那部分源码造成的。
    """
    report = make_report(
        {P2P[0]: Status.PASSED},
        collection_errors=(CollectionError("tests/test_a.py", "ImportError"),),
    )
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )

    assert verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert verdict.counts_as_infra_failure is False


def test_missing_with_protected_edit_raises_tampering_flag() -> None:
    """(b) AI 试图改受保护路径 + 出现 MISSING → 升级为疑似作弊（C-13c）。"""
    report = make_report({P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=report,
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(protected_path_edit_attempted=True),
    )

    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert ReviewFlag.TEST_TAMPERING_SUSPECTED in verdict.review_flags


def test_missing_without_evidence_goes_to_review() -> None:
    """(c) 原因不明 → 判 `UNRESOLVED` 并进人工复核，**不判作弊**（C-13a）。"""
    report = make_report({P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )

    assert verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert ReviewFlag.TEST_RESULT_INTEGRITY_SUSPECTED in verdict.review_flags
    assert ReviewFlag.TEST_TAMPERING_SUSPECTED not in verdict.review_flags


def test_protected_edit_always_triggers_review() -> None:
    """C-13d：碰过受保护路径就要复核，**即使最终判成修好**。"""
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=all_passing(),
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(protected_path_edit_attempted=True),
    )

    assert verdict.agent_outcome is AgentOutcome.RESOLVED
    assert ReviewFlag.PROTECTED_PATH_EDIT in verdict.review_flags
    assert verdict.needs_review is True


def test_success_without_a_report_is_a_harness_error() -> None:
    """平台说跑成功了却没有报告 —— 这是我们自己的 bug，不能算到 AI 头上。"""
    verdict = judge(infra_outcome=InfraOutcome.SUCCESS, report=None, fail_to_pass=F2P)

    assert verdict.infra_outcome is InfraOutcome.HARNESS_ERROR
    assert verdict.agent_outcome is None
    assert verdict.counts_as_infra_failure is True


# ── C-20：TEST_TIMEOUT 不许猜 ───────────────────────────────


def test_test_timeout_without_a_control_run_raises() -> None:
    """`TEST_TIMEOUT` 没跑对照组就判 → 抛异常，不猜。

    猜错的两个方向后果相反：猜"AI 的锅"会冤枉 AI，猜"平台的锅"会放过死循环。
    """
    with pytest.raises(ControlRunRequiredError):
        judge(infra_outcome=InfraOutcome.TEST_TIMEOUT, report=all_passing(), fail_to_pass=F2P)


def test_test_timeout_with_a_clean_control_run_blames_the_agent() -> None:
    """对照组正常、只有打了 AI 补丁才超时 → AI 多半写了死循环（C-20 第 4 步）。"""
    verdict = judge(
        infra_outcome=InfraOutcome.TEST_TIMEOUT,
        report=None,
        fail_to_pass=F2P,
        control_run_timed_out=False,
    )
    assert verdict.lifecycle_status is LifecycleStatus.COMPLETED
    assert verdict.agent_outcome is AgentOutcome.UNRESOLVED
    assert verdict.counts_as_infra_failure is False


def test_test_timeout_with_a_slow_control_run_invalidates_the_run() -> None:
    """对照组也超时 → 本次结果无效，计入平台故障率（C-20 第 5 步）。"""
    verdict = judge(
        infra_outcome=InfraOutcome.TEST_TIMEOUT,
        report=None,
        fail_to_pass=F2P,
        control_run_timed_out=True,
    )
    assert verdict.lifecycle_status is LifecycleStatus.FAILED
    assert verdict.agent_outcome is None
    assert verdict.counts_as_infra_failure is True


# ── 逐条用例记录 ────────────────────────────────────────────


def test_cases_carry_roles_and_missing_entries() -> None:
    """名单里有、报告里没有的记 MISSING；报告里有、名单里没有的记 OTHER。"""
    report = make_report({P2P[0]: Status.PASSED, "tests/test_a.py::test_extra": Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P, pass_to_pass=P2P
    )

    by_id = {c.test_id: c for c in verdict.cases}
    assert by_id[F2P[0]].role is Role.F2P
    assert by_id[F2P[0]].status is Status.MISSING
    assert by_id[P2P[0]].role is Role.P2P
    assert by_id["tests/test_a.py::test_extra"].role is Role.OTHER


def test_cases_are_sorted() -> None:
    """逐条记录要排序 —— 落库顺序抖动会让两次运行的结果 diff 出一堆假差异。"""
    report = make_report(
        {
            "tests/test_a.py::z": Status.PASSED,
            "tests/test_a.py::a": Status.PASSED,
            F2P[0]: Status.PASSED,
        }
    )
    verdict = judge(infra_outcome=InfraOutcome.SUCCESS, report=report, fail_to_pass=F2P)
    keys = [(c.role.value, c.test_id) for c in verdict.cases]
    assert keys == sorted(keys)


# ── AC 第二条：确定性 ───────────────────────────────────────


def test_judging_three_times_gives_identical_results() -> None:
    """同一份输入判 3 次，逐字段（含逐条用例状态）完全一致。

    判定有随机性的话，这个月的排行榜和下个月的排行榜就没法放在一起看，
    整个平台就失去意义了（`AGENTS.md` §5.1）。
    """
    report = make_report(
        {F2P[0]: Status.FAILED, P2P[0]: Status.PASSED, "tests/test_a.py::x": Status.SKIPPED}
    )
    facts = AgentFacts(protected_path_edit_attempted=True)
    verdicts = [
        judge(
            infra_outcome=InfraOutcome.SUCCESS,
            report=report,
            fail_to_pass=F2P,
            pass_to_pass=P2P,
            facts=facts,
        )
        for _ in range(3)
    ]

    assert verdicts[0] == verdicts[1] == verdicts[2]
    statuses = [tuple((c.test_id, c.status) for c in v.cases) for v in verdicts]
    assert statuses[0] == statuses[1] == statuses[2]


def test_review_flags_are_deduplicated() -> None:
    """同一个标记只挂一次 —— 复核任务按 (task_id, test_id, 错误摘要) 去重（C-13e），
    重复的标记会让去重键失效。"""
    report = make_report({P2P[0]: Status.PASSED})
    verdict = judge(
        infra_outcome=InfraOutcome.SUCCESS,
        report=report,
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        facts=AgentFacts(protected_path_edit_attempted=True),
    )
    assert len(verdict.review_flags) == len(set(verdict.review_flags))
