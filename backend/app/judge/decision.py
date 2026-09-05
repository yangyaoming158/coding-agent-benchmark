"""判定引擎（E4-T3，协议 C-14 的第 7 步、`06-judge-attribution.md` §11.2）。

一句话：**把"逐条用例是什么状态"变成"这次评测算什么结果"。**

    infra_outcome ──┐
    ParsedReport  ──┼──▶ judge() ──▶ Verdict（三字段 + 逐条用例 + 复核标记）
    AgentFacts    ──┘

## 第一条原则：完全确定，禁止用大模型

`judge()` 是**纯函数** —— 同样的输入永远给同样的输出，没有时间、随机数、
文件系统、网络。`AGENTS.md` §5.1 说得很直白：同一个补丁，今天判和下个月判
必须得到一样的结论，否则排行榜不可比。

大模型只能用在"分析它为什么没修好"（E6 归因），而且**输出不许回写判定结果**。

## 三个字段各管一件事

- `lifecycle_status`：走到哪一步了（终态只有 COMPLETED / FAILED / CANCELLED）
- `infra_outcome`：**平台**有没有正确完成这次评测
- `agent_outcome`：**被测 AI** 有没有把 bug 修好

把"AI 失败"和"平台故障"混进同一个字段，解决率就不可信了。

## 怎么从 infra_outcome 得到 agent_outcome

**查 `INFRA_TO_AGENT_MAPPING`，不写 if**（协议 C-19 明令禁止把这张表的逻辑
散落到各处的 if 分支里）。那张表在 `app.domain.protocol`，E0 就建好了，这里只查。

表里有三条不是固定取值，必须再看别的证据：

| 规则 | 要什么证据 | 谁提供 |
|:---|:---|:---|
| `BY_TEST_RESULT` | 逐条用例状态 | `ParsedReport` |
| `BY_AGENT_STARTED` | AI 到底启动过没有 | `AgentFacts.agent_started` |
| `BY_CONTROL_RUN` | 不打补丁的对照组超不超时 | 调用方按 C-20 跑完再告诉我们 |

`BY_CONTROL_RUN` 那条（`TEST_TIMEOUT`）**不传结论就抛异常**，不猜。C-20 写死了
那套流程（先重跑、再跑对照组），它要起容器，不是判定引擎该干的事；
而猜错的方向恰好相反 —— 猜"AI 的锅"会冤枉 AI，猜"平台的锅"会放过死循环。

## RESOLVED 和 EMPTY_PATCH 谁优先：RESOLVED

空补丁 + F2P 全过时，两条判定条件同时成立（C-08 的 RESOLVED 行不看补丁）。
这里**判 RESOLVED**，因为 Noop 哨兵正是靠这个发现坏题的：

> Noop 哨兵：用空补丁跑整个数据集，解决率必须 0%。不是 0% 说明有的题目
> 在修复前测试就已经通过了。

判成 `EMPTY_PATCH` 的话，坏题会让哨兵显示 0%（看着很正常），
于是**一道修复前就通过的坏题永远不会被发现**。

## MISSING 不等于作弊

出现 `MISSING` 时先跑 C-13b 的三项自检，再按 C-13 分三支，**顺序不能反**：

- **(a) 报告不完整、或归一化对不上** —— 平台自己的问题。
  判 `FAILED` + `HARNESS_ERROR`，`agent_outcome` 留空，计入平台故障率，**不罚 AI**。
- **(b) 有实际证据表明补丁破坏了测试收集** —— 判 `COMPLETED` + `UNRESOLVED`。
- **(c) 原因不明** —— 判 `COMPLETED` + `UNRESOLVED`，并进人工复核。

C-13a 明令禁止仅凭 `MISSING` 判作弊 —— 用例 ID 归一化写错本身就会制造大量假
`MISSING`，把它当作弊证据会产生大量冤枉的指控，而且会掩盖真正的原因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import (
    AgentOutcome,
    InfraOutcome,
    LifecycleStatus,
    TestRole,
    TestStatus,
)
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    FaultOwner,
    InfraFailureCounting,
    OutcomeRule,
    assert_legal_combination,
)
from app.judge.report_parser import IntegrityCheck, ParsedReport

#: 只有这些状态算"通过"。**禁止**把 MISSING / SKIPPED / XFAIL 算进来（协议 C-12）。
#:
#: 写成一个显式集合而不是 `status is PASSED`，是为了让"哪些算通过"这件事有一个
#: 能被搜索、能被单测钉住的地方。XPASS 也不算：一条本该失败却通过了的用例，
#: 说明题目或标记有问题，不能当作修好的证据。
PASSING_STATUSES: frozenset[TestStatus] = frozenset({TestStatus.PASSED})


class ReviewFlag(StrEnum):
    """要人看一眼的理由。判定结果照常产出，这些只是额外挂上去的标记。"""

    #: 出现 MISSING 且查不出原因（C-13 的 (c) 分支）。一次实验里超过 5% 的题
    #: 落到这里，整个实验要标记为"需人工确认才能发布"（C-13f）。
    TEST_RESULT_INTEGRITY_SUSPECTED = "TEST_RESULT_INTEGRITY_SUSPECTED"
    #: 有实际证据表明 AI 动了测试（C-13c）。
    TEST_TAMPERING_SUSPECTED = "TEST_TAMPERING_SUSPECTED"
    #: AI 试图改受保护路径。**即使最终没出现 MISSING 也要复核**（C-13d）。
    PROTECTED_PATH_EDIT = "PROTECTED_PATH_EDIT"


@dataclass(frozen=True, slots=True)
class AgentFacts:
    """判定要用到的、来自被测 AI 那一侧的事实。

    全部由上游提供，判定引擎不去推断 —— 推断就等于在判定里引入了第二个真相来源。
    """

    #: `agent_started_at` 是不是非空。C-69 规定 `NOT_ATTEMPTED` 当且仅当它为空。
    agent_started: bool = True
    #: AI 是正常退出的，还是被超时/崩溃打断的。C-08c 靠它区分 EMPTY_PATCH 和 UNRESOLVED。
    exited_normally: bool = True
    #: 标准化之后的补丁是不是空的（C-08a：**不等于**"AI 什么都没做"）。
    normalized_patch_empty: bool = False
    #: 过滤之前的原始改动是不是空的（C-08b 的诊断字段）。
    raw_patch_empty: bool = False
    #: AI 有没有试图改受保护路径（C-08b、C-13c、C-13d）。
    #: 由 E4-T2 的 `ProtectedPathRestore.attempted` 提供。
    protected_path_edit_attempted: bool = False


@dataclass(frozen=True, slots=True)
class CaseVerdict:
    """一条用例的最终记录，字段和 `test_results` 表的列一一对应。"""

    test_id: str
    role: TestRole
    status: TestStatus
    duration_ms: int | None = None
    message_excerpt: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in PASSING_STATUSES


@dataclass(frozen=True, slots=True)
class Verdict:
    """一次评测的最终结论。

    三字段组合在构造时就已经过 `assert_legal_combination` 校验（C-78），
    所以拿到 `Verdict` 就可以直接落库，不用再查一遍。
    """

    lifecycle_status: LifecycleStatus
    infra_outcome: InfraOutcome
    agent_outcome: AgentOutcome | None
    #: 这次算不算平台故障（进 C-21 的平台故障率分子）。
    counts_as_infra_failure: bool
    #: 逐条用例的结果，按 (role, test_id) 排序 —— 落库顺序稳定，两次运行的 diff 才干净。
    cases: tuple[CaseVerdict, ...]
    #: F2P 全过没有。没跑测试时为 False。
    f2p_ok: bool = False
    #: P2P 全过没有。没跑测试时为 False。
    p2p_ok: bool = False
    review_flags: tuple[ReviewFlag, ...] = ()
    #: 出现 MISSING 时的 C-13b 三项自检结果，没出现就是 None。
    integrity: IntegrityCheck | None = None
    #: 这个结论是怎么来的，人话。进制品和复核任务。
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.agent_outcome is AgentOutcome.RESOLVED

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags)


#: 责任方 → 终态。这张表也是从协议 C-68 的六行合法组合读出来的，不是另立规矩：
#:
#: - 责任在 AI（超时、自身崩溃、补丁打不上）→ `COMPLETED`。**拿到了结论**，
#:   结论就是"它没修好"。这是最容易写错的一条 —— 直觉会觉得"出错了就是 FAILED"，
#:   可那样一来 AI 只要把自己搞崩就能从解决率的分母里消失。
#: - 责任在平台 / 题目 / 外部服务 → `FAILED`，没拿到结论。
#: - 人工取消 → `CANCELLED`。
#:
#: `NONE`（infra_outcome=SUCCESS）和 `BY_CONTROL_RUN`（TEST_TIMEOUT）不在表里：
#: 前者要看逐条用例，后者要看对照组，都由各自的分支单独处理。
_LIFECYCLE_BY_OWNER: dict[FaultOwner, LifecycleStatus] = {
    FaultOwner.AGENT: LifecycleStatus.COMPLETED,
    FaultOwner.PLATFORM: LifecycleStatus.FAILED,
    FaultOwner.PLATFORM_OR_TASK: LifecycleStatus.FAILED,
    FaultOwner.EXTERNAL: LifecycleStatus.FAILED,
    FaultOwner.HUMAN: LifecycleStatus.CANCELLED,
}


class ControlRunRequiredError(RuntimeError):
    """`TEST_TIMEOUT` 必须先跑完 C-20 的对照流程才能判。

    这不是"暂时没实现"，是**故意不猜**：C-20 写死了先重跑、再跑不打补丁的对照组，
    对照组正常才算 AI 写了死循环，对照组也超时就说明本次结果无效。
    两个方向的误判后果相反 —— 猜前者会冤枉 AI，猜后者会放过死循环。
    """


def _classify_cases(
    report: ParsedReport,
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str],
) -> tuple[CaseVerdict, ...]:
    """把报告和题目的名单对起来，产出逐条记录。

    题目里列了、报告里找不到的记 `MISSING`（C-11）。报告里有、名单里没有的记
    `OTHER` —— 不参与判定，但存下来备查（`TestRole` 的定义就是这么写的）。

    用 `report.resolve()` 而不是直接下标：它还会试备选 ID 和路径后缀，
    那正是防假 `MISSING` 的地方。
    """
    cases: list[CaseVerdict] = []
    claimed: set[str] = set()
    for role, ids in ((TestRole.F2P, fail_to_pass), (TestRole.P2P, pass_to_pass)):
        for test_id in ids:
            found = report.resolve(test_id)
            if found is None:
                cases.append(CaseVerdict(test_id, role, TestStatus.MISSING))
                continue
            claimed.add(found.test_id)
            cases.append(
                CaseVerdict(
                    found.test_id, role, found.status, found.duration_ms, found.message_excerpt
                )
            )
    for test_id, case in report.cases.items():
        if test_id not in claimed:
            cases.append(
                CaseVerdict(
                    test_id, TestRole.OTHER, case.status, case.duration_ms, case.message_excerpt
                )
            )
    # 排序让落库顺序稳定：顺序抖动会让两次运行的结果 diff 出一堆假差异
    return tuple(sorted(cases, key=lambda c: (c.role.value, c.test_id)))


def _role_ok(cases: Sequence[CaseVerdict], role: TestRole) -> bool:
    """这一组用例是不是全过了。

    **一条都没有时算不过。** 一道 F2P 为空的题不该被判成修好 —— 那是坏题，
    而"空集全称命题为真"会让它静悄悄地进排行榜。
    """
    picked = [c for c in cases if c.role is role]
    return bool(picked) and all(c.passed for c in picked)


def _missing_branch(
    integrity: IntegrityCheck,
    report: ParsedReport,
    facts: AgentFacts,
) -> tuple[str, ReviewFlag | None, str]:
    """C-13 的三分支：出现 MISSING 时该怪谁。

    返回 (分支代号, 复核标记, 人话理由)。**先自检再分支**，顺序反了就会把平台
    自己的 bug 算到被测 AI 头上（C-13a）。
    """
    if integrity.blames_harness:
        return (
            "a",
            None,
            f"报告本身有问题（{integrity.report_problem or '归一化对不上'}），判平台故障，不罚 AI",
        )
    # (b) 要**实际证据**（C-13c）：收集阶段报错，或者 AI 确实伸手碰过受保护路径。
    # 收集错误在这里算 AI 的锅是有前提的：受保护路径已经被强制还原过（E4-T2），
    # 官方测试是完好的，那 import 挂掉只能是 AI 改的那部分源码造成的。
    evidence = []
    if report.collection_errors:
        evidence.append(f"测试收集报错：{[e.module_path for e in report.collection_errors]}")
    if facts.protected_path_edit_attempted:
        evidence.append("AI 试图修改受保护路径")
    if evidence:
        flag = ReviewFlag.TEST_TAMPERING_SUSPECTED if facts.protected_path_edit_attempted else None
        return "b", flag, "；".join(evidence) + " —— 判 AI 没修好"
    return (
        "c",
        ReviewFlag.TEST_RESULT_INTEGRITY_SUSPECTED,
        f"有 {len(integrity.missing_ids)} 条用例在报告里找不到，原因不明，进人工复核",
    )


def judge(
    *,
    infra_outcome: InfraOutcome,
    report: ParsedReport | None,
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str] = (),
    facts: AgentFacts | None = None,
    control_run_timed_out: bool | None = None,
) -> Verdict:
    """判定一次评测的结果。**纯函数**，同样的输入永远给同样的输出。

    `control_run_timed_out` 只在 `infra_outcome = TEST_TIMEOUT` 时需要，
    由调用方按 C-20 跑完对照组之后告诉我们：True = 不打补丁也超时（本次结果无效），
    False = 只有打了 AI 补丁才超时（AI 多半写了死循环）。不传就抛
    `ControlRunRequiredError`。
    """
    facts = facts or AgentFacts()
    rule = INFRA_TO_AGENT_MAPPING[infra_outcome]
    flags: list[ReviewFlag] = []
    # C-13d：碰过受保护路径本身就要复核，**即使最终没出现 MISSING**
    if facts.protected_path_edit_attempted:
        flags.append(ReviewFlag.PROTECTED_PATH_EDIT)

    counts = rule.counts_as_infra_failure is InfraFailureCounting.YES

    if rule.outcome_rule is OutcomeRule.BY_TEST_RESULT:
        return _judge_by_tests(report, fail_to_pass, pass_to_pass, facts, flags)

    if rule.outcome_rule is OutcomeRule.BY_CONTROL_RUN:
        if control_run_timed_out is None:
            raise ControlRunRequiredError(
                f"{infra_outcome.value} 要按协议 C-20 先重跑、再跑不打补丁的对照组，"
                f"把结论用 control_run_timed_out 传进来。判定引擎不猜。"
            )
        if control_run_timed_out:
            # 对照组也超时 → 本次结果无效，计入平台故障率（C-20 第 5 步）
            return _finish(
                LifecycleStatus.FAILED,
                infra_outcome,
                None,
                True,
                (),
                flags,
                reason="不打补丁的对照组也超时，本次结果无效",
                agent_started=facts.agent_started,
            )
        # 只有打了 AI 补丁才超时 → AI 的问题，不计入平台故障率（C-20 第 4 步）
        return _finish(
            LifecycleStatus.COMPLETED,
            infra_outcome,
            AgentOutcome.UNRESOLVED,
            False,
            (),
            flags,
            reason="对照组正常、只有打了 AI 补丁才超时，判 AI 没修好",
            agent_started=facts.agent_started,
        )

    lifecycle = _LIFECYCLE_BY_OWNER[rule.owner]

    if rule.outcome_rule is OutcomeRule.BY_AGENT_STARTED:
        # C-69：NOT_ATTEMPTED 当且仅当 AI 从未启动
        outcome = None if facts.agent_started else AgentOutcome.NOT_ATTEMPTED
        return _finish(
            lifecycle,
            infra_outcome,
            outcome,
            counts,
            (),
            flags,
            reason=f"平台故障 {infra_outcome.value}，"
            + ("AI 已启动但没拿到结论" if facts.agent_started else "AI 从未启动"),
            agent_started=facts.agent_started,
        )

    fixed: AgentOutcome | None = {
        OutcomeRule.FIXED_UNRESOLVED: AgentOutcome.UNRESOLVED,
        OutcomeRule.FIXED_INVALID_PATCH: AgentOutcome.INVALID_PATCH,
        OutcomeRule.FIXED_NOT_ATTEMPTED: AgentOutcome.NOT_ATTEMPTED,
        OutcomeRule.FIXED_NULL: None,
    }[rule.outcome_rule]
    return _finish(
        lifecycle,
        infra_outcome,
        fixed,
        counts,
        (),
        flags,
        reason=f"按 C-18 映射表：{infra_outcome.value} → {fixed or 'NULL'}",
        agent_started=facts.agent_started,
    )


def _judge_by_tests(
    report: ParsedReport | None,
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str],
    facts: AgentFacts,
    flags: list[ReviewFlag],
) -> Verdict:
    """`infra_outcome = SUCCESS` 时按逐条用例结果判（C-08）。"""
    if report is None:
        # 平台说跑成功了却没有报告 —— 这是我们自己的 bug，不能算到 AI 头上
        return _finish(
            LifecycleStatus.FAILED,
            InfraOutcome.HARNESS_ERROR,
            None,
            True,
            (),
            flags,
            reason="infra_outcome=SUCCESS 却没有测试报告，属于平台自身错误",
            agent_started=facts.agent_started,
        )

    cases = _classify_cases(report, fail_to_pass, pass_to_pass)
    f2p_ok = _role_ok(cases, TestRole.F2P)
    # P2P 允许为空（题目可以不设回归检查范围），空的时候视为通过
    p2p_ok = not pass_to_pass or _role_ok(cases, TestRole.P2P)

    integrity = None
    if any(c.status is TestStatus.MISSING for c in cases):
        integrity = report.check_integrity([*fail_to_pass, *pass_to_pass])
        branch, flag, reason = _missing_branch(integrity, report, facts)
        if flag is not None:
            flags.append(flag)
        if branch == "a":
            # 平台或解析器的问题 → 不罚 AI（C-13 的 (a) 分支）
            return _finish(
                LifecycleStatus.FAILED,
                InfraOutcome.HARNESS_ERROR,
                None,
                True,
                cases,
                flags,
                f2p_ok=f2p_ok,
                p2p_ok=p2p_ok,
                integrity=integrity,
                reason=reason,
                agent_started=facts.agent_started,
            )
        return _finish(
            LifecycleStatus.COMPLETED,
            InfraOutcome.SUCCESS,
            AgentOutcome.UNRESOLVED,
            False,
            cases,
            flags,
            f2p_ok=f2p_ok,
            p2p_ok=p2p_ok,
            integrity=integrity,
            reason=reason,
            agent_started=facts.agent_started,
        )

    if f2p_ok and p2p_ok:
        # RESOLVED 优先于 EMPTY_PATCH：Noop 哨兵靠"空补丁也能判 RESOLVED"发现坏题。
        # 反过来判 EMPTY_PATCH 的话，一道修复前就通过的坏题会让哨兵显示 0%，
        # 看着完全正常，于是永远不会被发现。
        return _finish(
            LifecycleStatus.COMPLETED,
            InfraOutcome.SUCCESS,
            AgentOutcome.RESOLVED,
            False,
            cases,
            flags,
            f2p_ok=True,
            p2p_ok=True,
            reason="F2P 全过且 P2P 全过",
            agent_started=facts.agent_started,
        )

    if facts.normalized_patch_empty and facts.exited_normally:
        # C-08c：正常退出 + 空补丁 → EMPTY_PATCH；异常终止 + 空补丁 → UNRESOLVED。
        # 混在一起会让空补丁率这个诊断指标失去意义 —— 超时被杀的 AI 可能正要写文件。
        return _finish(
            LifecycleStatus.COMPLETED,
            InfraOutcome.SUCCESS,
            AgentOutcome.EMPTY_PATCH,
            False,
            cases,
            flags,
            f2p_ok=f2p_ok,
            p2p_ok=p2p_ok,
            reason="AI 正常退出但标准化后的补丁为空",
            agent_started=facts.agent_started,
        )

    failing = [c.test_id for c in cases if c.role is TestRole.F2P and not c.passed]
    broken = [c.test_id for c in cases if c.role is TestRole.P2P and not c.passed]
    return _finish(
        LifecycleStatus.COMPLETED,
        InfraOutcome.SUCCESS,
        AgentOutcome.UNRESOLVED,
        False,
        cases,
        flags,
        f2p_ok=f2p_ok,
        p2p_ok=p2p_ok,
        reason=f"F2P 未全过{failing or ''}" if not f2p_ok else f"P2P 被改坏{broken}",
        agent_started=facts.agent_started,
    )


def _finish(
    lifecycle_status: LifecycleStatus,
    infra_outcome: InfraOutcome,
    agent_outcome: AgentOutcome | None,
    counts_as_infra_failure: bool,
    cases: tuple[CaseVerdict, ...],
    flags: Sequence[ReviewFlag],
    *,
    f2p_ok: bool = False,
    p2p_ok: bool = False,
    integrity: IntegrityCheck | None = None,
    reason: str = "",
    agent_started: bool | None = None,
) -> Verdict:
    """组装 `Verdict`，落库前先把三字段组合校验一遍（协议 C-78）。

    校验放在这里而不是调用方：判定引擎是这三个字段的唯一产地，
    在出口处拦一次，比让每个下游各查一遍可靠。非法组合是程序 bug，
    **禁止静默落库**。
    """
    assert_legal_combination(lifecycle_status, infra_outcome, agent_outcome, agent_started)
    return Verdict(
        lifecycle_status=lifecycle_status,
        infra_outcome=infra_outcome,
        agent_outcome=agent_outcome,
        counts_as_infra_failure=counts_as_infra_failure,
        cases=cases,
        f2p_ok=f2p_ok,
        p2p_ok=p2p_ok,
        review_flags=tuple(dict.fromkeys(flags)),
        integrity=integrity,
        reason=reason,
    )


__all__ = [
    "PASSING_STATUSES",
    "AgentFacts",
    "CaseVerdict",
    "ControlRunRequiredError",
    "ReviewFlag",
    "Verdict",
    "judge",
]
