"""规则前置分类器的单测（E6-T1）。

三件事：

1. **穷举**：13 个 `InfraOutcome` 每一个都有确定归类。往枚举里加值，这里立刻变红。
2. 四类 F6 / F7 / F8 / N1 各判得对。
3. 边界不出幺蛾子：一条用例都没跑、`agent_outcome` 是空、人工取消、要跑对照组。

穷举那条是抄 `test_judge_decision.py` 的做法。挑几个典型值测，等于把
"将来有人往枚举里加了一个值却忘了归类"这件事完全交给运气。
"""

from __future__ import annotations

import pytest

from app.attribution.rules import (
    RuleName,
    RuleSkip,
    RuleVerdict,
    RunFacts,
    SkipReason,
    classify,
)
from app.domain.enums import AgentOutcome, FailureCategory, InfraOutcome
from app.domain.protocol import INFRA_TO_AGENT_MAPPING, OutcomeRule

#: 13 个 `InfraOutcome` 各自该被归到哪。
#: 值是 `FailureCategory`（规则判死了）或 `SkipReason`（规则判不了，说明理由）。
#: 这张表和 `INFRA_TO_AGENT_MAPPING` 是两回事：那张表说"算谁的"，这张表说"归哪类"。
EXPECTED: dict[InfraOutcome, FailureCategory | SkipReason] = {
    # 平台自己没坏 —— 归哪类要看测试结果，规则给不出，交给大模型
    InfraOutcome.SUCCESS: SkipReason.NEEDS_LLM,
    # 责任方是 AI
    InfraOutcome.AGENT_TIMEOUT: FailureCategory.F8_AGENT_TOOL_OR_BUDGET_FAILURE,
    InfraOutcome.AGENT_RUNTIME_ERROR: FailureCategory.F8_AGENT_TOOL_OR_BUDGET_FAILURE,
    # 补丁打不上：责任方也是 AI，但 §12.1 把它划进 F7 不是 F8
    InfraOutcome.PATCH_APPLY_FAILED: FailureCategory.F7_EMPTY_OR_INVALID_PATCH,
    # 计入平台故障率的，全是 N1
    InfraOutcome.ENV_BUILD_FAILED: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.WORKSPACE_ERROR: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.SANDBOX_ERROR: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.OOM_KILLED: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.TEST_DISCOVERY_ERROR: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.HARNESS_ERROR: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    InfraOutcome.AGENT_AUTH_ERROR: FailureCategory.N1_INFRASTRUCTURE_FAILURE,
    # 要按协议 C-20 跑对照组才知道算谁的（E4-T5 还没做）
    InfraOutcome.TEST_TIMEOUT: SkipReason.NEEDS_CONTROL_RUN,
    # 人喊停的，不算失败
    InfraOutcome.CANCELLED: SkipReason.CANCELLED,
}


def facts(**kwargs: object) -> RunFacts:
    """默认是一次"跑完了、没修好、也没回归"的普通失败。"""
    base: dict[str, object] = {
        "infra_outcome": InfraOutcome.SUCCESS,
        "agent_outcome": AgentOutcome.UNRESOLVED,
        "f2p_passed": 0,
        "f2p_total": 3,
        "p2p_passed": 100,
        "p2p_total": 100,
        "raw_patch_empty": False,
        "protected_path_edit_attempted": False,
    }
    base.update(kwargs)
    return RunFacts(**base)  # type: ignore[arg-type]


def test_every_infra_outcome_has_a_definite_class() -> None:
    """穷举。加了新枚举值而没在这里补一行，测试直接红。"""
    assert set(EXPECTED) == set(InfraOutcome), "EXPECTED 和 InfraOutcome 对不上"


#: 协议 C-18 的 `outcome_rule` → 这一格真实会落到的 `agent_outcome`。
#: 穷举时必须按这张表给 `agent_outcome`，否则会造出协议里根本不存在的组合 ——
#: 第一版就栽在这：给 `PATCH_APPLY_FAILED` 配了 `UNRESOLVED`，
#: 而 C-18 规定它只能是 `INVALID_PATCH`，于是 F7 没触发、落到了 F8。
OUTCOME_FOR_RULE: dict[OutcomeRule, AgentOutcome | None] = {
    OutcomeRule.BY_TEST_RESULT: AgentOutcome.UNRESOLVED,  # 取失败那一支
    OutcomeRule.BY_AGENT_STARTED: None,  # 已启动 → 按 C-69 是 NULL
    OutcomeRule.BY_CONTROL_RUN: None,
    OutcomeRule.FIXED_UNRESOLVED: AgentOutcome.UNRESOLVED,
    OutcomeRule.FIXED_INVALID_PATCH: AgentOutcome.INVALID_PATCH,
    OutcomeRule.FIXED_NOT_ATTEMPTED: AgentOutcome.NOT_ATTEMPTED,
    OutcomeRule.FIXED_NULL: None,
}


def test_every_outcome_rule_maps_to_a_real_agent_outcome() -> None:
    assert set(OUTCOME_FOR_RULE) == set(OutcomeRule), "OUTCOME_FOR_RULE 和 OutcomeRule 对不上"


@pytest.mark.parametrize("infra_outcome", list(InfraOutcome))
def test_exhaustive_over_infra_outcome(infra_outcome: InfraOutcome) -> None:
    rule = INFRA_TO_AGENT_MAPPING[infra_outcome]
    result = classify(
        facts(
            infra_outcome=infra_outcome,
            agent_outcome=OUTCOME_FOR_RULE[rule.outcome_rule],
        )
    )
    expected = EXPECTED[infra_outcome]
    if isinstance(expected, FailureCategory):
        assert isinstance(result, RuleVerdict), f"{infra_outcome} 应该判死"
        assert result.category is expected
    else:
        assert isinstance(result, RuleSkip), f"{infra_outcome} 应该判不了"
        assert result.reason is expected


def test_f6_regression_f2p_all_pass_but_p2p_broken() -> None:
    result = classify(facts(f2p_passed=3, f2p_total=3, p2p_passed=98, p2p_total=100))
    assert isinstance(result, RuleVerdict)
    assert result.category is FailureCategory.F6_REGRESSION
    assert result.rule is RuleName.REGRESSION
    assert result.evidence["f2p"] == "3/3"
    assert result.evidence["p2p"] == "98/100"


def test_f7_empty_patch() -> None:
    result = classify(facts(agent_outcome=AgentOutcome.EMPTY_PATCH, raw_patch_empty=True))
    assert isinstance(result, RuleVerdict)
    assert result.category is FailureCategory.F7_EMPTY_OR_INVALID_PATCH
    assert result.evidence["empty_kind"] == "nothing_changed"


def test_f7_protected_paths_only() -> None:
    """作弊未遂（协议 C-08b）。和"真的一个字没改"要分得开。"""
    result = classify(
        facts(
            agent_outcome=AgentOutcome.EMPTY_PATCH,
            raw_patch_empty=False,
            protected_path_edit_attempted=True,
        )
    )
    assert isinstance(result, RuleVerdict)
    assert result.evidence["empty_kind"] == "protected_paths_only"


def test_f8_timeout_beats_empty_patch() -> None:
    """AGENT_TIMEOUT 按 C-09a 判 UNRESOLVED，补丁可以是空的，但这是 F8 不是 F7。"""
    result = classify(
        facts(
            infra_outcome=InfraOutcome.AGENT_TIMEOUT,
            agent_outcome=AgentOutcome.UNRESOLVED,
            raw_patch_empty=True,
        )
    )
    assert isinstance(result, RuleVerdict)
    assert result.category is FailureCategory.F8_AGENT_TOOL_OR_BUDGET_FAILURE


def test_infra_failure_beats_empty_patch() -> None:
    """顺序：平台自己坏了的时候，不许把账算到被测 AI 头上。"""
    result = classify(
        facts(
            infra_outcome=InfraOutcome.OOM_KILLED,
            agent_outcome=AgentOutcome.EMPTY_PATCH,
            raw_patch_empty=True,
        )
    )
    assert isinstance(result, RuleVerdict)
    assert result.category is FailureCategory.N1_INFRASTRUCTURE_FAILURE


def test_resolved_is_not_attributed() -> None:
    result = classify(facts(agent_outcome=AgentOutcome.RESOLVED, f2p_passed=3))
    assert isinstance(result, RuleSkip)
    assert result.reason is SkipReason.RESOLVED


def test_unfinished_is_not_attributed() -> None:
    result = classify(facts(infra_outcome=None, agent_outcome=None))
    assert isinstance(result, RuleSkip)
    assert result.reason is SkipReason.NOT_FINISHED


def test_zero_cases_is_not_a_regression() -> None:
    """f2p_total = 0 时"F2P 全过"是句空话，不能当回归。"""
    result = classify(facts(f2p_passed=0, f2p_total=0, p2p_passed=0, p2p_total=0))
    assert isinstance(result, RuleSkip)
    assert result.reason is SkipReason.NEEDS_LLM


def test_empty_p2p_is_not_a_regression() -> None:
    result = classify(facts(f2p_passed=3, f2p_total=3, p2p_passed=0, p2p_total=0))
    assert isinstance(result, RuleSkip)
    assert result.reason is SkipReason.NEEDS_LLM


def test_null_counters_are_not_a_regression() -> None:
    result = classify(facts(f2p_passed=None, p2p_passed=None))
    assert isinstance(result, RuleSkip)


def test_null_agent_outcome_still_classifies_as_n1() -> None:
    """协议 C-18：OOM_KILLED 时 agent_outcome 必然是 NULL。"""
    result = classify(facts(infra_outcome=InfraOutcome.OOM_KILLED, agent_outcome=None))
    assert isinstance(result, RuleVerdict)
    assert result.category is FailureCategory.N1_INFRASTRUCTURE_FAILURE


def test_same_facts_give_the_same_verdict() -> None:
    """纯函数。同一个补丁今天判和下个月判必须一样（AGENTS.md §5.1 同一条理由）。"""
    f = facts(f2p_passed=3, f2p_total=3, p2p_passed=98, p2p_total=100)
    assert classify(f) == classify(f)
