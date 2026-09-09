"""LLM 预筛的 prompt、解析与分流（E1-T5，`03-benchmark-spec.md` §8.4）。

**这一组不调模型。** 真正调模型只有一行（`client.complete`），
靠不住的是它两头：喂进去的东西对不对、拿回来的东西怎么解读、按什么规则分流。

分流规则错了的代价：把一条泄题的候选放过去，就等于拿答案考被测 AI ——
那道题会被所有 Agent 满分通过，而排行榜上看不出任何异常。
所以"泄题一票否决"这条单独测。
"""

from __future__ import annotations

import pytest

from app.benchmark.prescreen import (
    MAX_ISSUE_CHARS,
    MIN_ISSUE_BODY_CHARS,
    PROMPT_VERSION,
    PrescreenInput,
    parse_verdict,
    rule_flags,
    score_histogram,
)

GOOD = {
    "self_contained": True,
    "leaks_fix": False,
    "over_specified": False,
    "locatable": True,
    "score": 5,
    "reason": "复现步骤清楚，没有泄题",
}


def verdict(**overrides: object):  # type: ignore[no-untyped-def]
    return parse_verdict({**GOOD, **overrides})


# ══════════════════════════════════════════════════════════════
# 解析模型的回答
# ══════════════════════════════════════════════════════════════


def test_a_normal_answer_parses() -> None:
    v = verdict()
    assert v.score == 5.0
    assert v.self_contained is True
    assert v.reason == "复现步骤清楚，没有泄题"


def test_a_missing_score_is_an_error_not_a_default() -> None:
    """分数是分流的唯一依据。编一个默认值出来等于假装模型回答过。"""
    with pytest.raises(ValueError, match="没有 score"):
        parse_verdict({"self_contained": True})


def test_a_non_numeric_score_is_an_error() -> None:
    with pytest.raises(ValueError, match="不是数字"):
        parse_verdict({"score": "很好"})


def test_a_non_object_answer_is_an_error() -> None:
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_verdict([1, 2, 3])


def test_an_out_of_range_score_is_clamped_not_rejected() -> None:
    """模型偶尔给 7 分或 -1 分。夹住而不是报错 —— 夹完仍然落在正确的档里，
    为一条越界的分数丢掉整批回答不值得。
    """
    assert verdict(score=9).score == 5.0
    assert verdict(score=-3).score == 0.0


def test_string_and_numeric_booleans_are_understood() -> None:
    """模型有时把布尔写成 `"true"` / `"yes"` / `1`。都认。"""
    assert parse_verdict({**GOOD, "leaks_fix": "true"}).leaks_fix is True
    assert parse_verdict({**GOOD, "leaks_fix": 1}).leaks_fix is True
    assert parse_verdict({**GOOD, "leaks_fix": "no"}).leaks_fix is False


def test_missing_booleans_fall_back_to_the_conservative_side() -> None:
    """漏答只该让候选走向人工复核，不该让一条烂题直接通过。

    `self_contained` 和 `locatable` 缺了按 False 算 —— 那会拉低判断，
    而不是放行。
    """
    v = parse_verdict({"score": 5})
    assert v.self_contained is False
    assert v.locatable is False
    assert v.leaks_fix is False, "泄题缺省按'没泄'，否则模型漏答就把好题全毙了"


# ══════════════════════════════════════════════════════════════
# 分流（§8.4：<2 丢弃；2~4 人工；≥4 通过）
# ══════════════════════════════════════════════════════════════


def test_a_high_score_passes() -> None:
    assert verdict(score=5).decision == "PASS"
    assert verdict(score=4).decision == "PASS"


def test_a_middling_score_goes_to_human_review() -> None:
    assert verdict(score=3).decision == "REVIEW"
    assert verdict(score=2).decision == "REVIEW"


def test_a_low_score_is_rejected() -> None:
    assert verdict(score=1).decision == "REJECT"
    assert verdict(score=0).decision == "REJECT"


def test_a_leaking_issue_is_rejected_no_matter_how_high_the_score() -> None:
    """**泄题一票否决。**

    模型说"issue 里已经给了修复方案"而我们还把它当题目，那就是拿答案考人 ——
    那道题会被所有 Agent 满分通过，而排行榜上看不出任何异常。

    这一条比分数硬，因为分数是连续量、可以擦边，泄不泄题是个是非题。
    """
    assert verdict(score=5, leaks_fix=True).decision == "REJECT"


def test_a_rule_flag_pulls_a_passing_score_down_to_review() -> None:
    """规则标记（正文太短之类）是确定的事实，比模型的印象分硬。"""
    v = parse_verdict(GOOD, flags=("正文不足 200 字",))
    assert v.score == 5.0
    assert v.decision == "REVIEW"


# ══════════════════════════════════════════════════════════════
# 不用问模型就能判的几条
# ══════════════════════════════════════════════════════════════


def test_a_short_body_is_flagged() -> None:
    """ "正文有没有 200 字"是数出来的，问模型只会浪费 token 还可能数错。"""
    flags = rule_flags(body="不工作", f2p_candidates=3)
    assert any("200" in f for f in flags)


def test_a_long_enough_body_is_not_flagged() -> None:
    assert rule_flags(body="x" * MIN_ISSUE_BODY_CHARS, f2p_candidates=3) == ()


def test_no_f2p_candidate_is_flagged() -> None:
    """一条候选 F2P 都抽不出来，多半是 test_patch 劈错了或者 PR 只改了 fixture。"""
    flags = rule_flags(body="x" * 300, f2p_candidates=0)
    assert flags == ("抽不出候选 F2P",)


def test_too_many_f2p_candidates_is_flagged() -> None:
    """§7.9 的 `review_flags`：F2P 超过 20 条要人看一眼。"""
    flags = rule_flags(body="x" * 300, f2p_candidates=25)
    assert any("超过 20" in f for f in flags)


def test_a_leak_that_survived_redaction_is_flagged() -> None:
    """脱敏没脱干净 —— 这条必须让人看见，不能只靠模型的印象分。"""
    flags = rule_flags(body="x" * 300, f2p_candidates=3, leaks=["仓库 PR/issue 链接"])
    assert flags == ("脱敏后仍有仓库 PR/issue 链接",)


# ══════════════════════════════════════════════════════════════
# 喂给模型的东西
# ══════════════════════════════════════════════════════════════


def make_input(**overrides: object) -> PrescreenInput:
    base = {
        "repo": "pallets/click",
        "pr_number": 2946,
        "title": "Path 类型没有去掉首尾空格",
        "body": "复现步骤见下",
        "code_paths": ("src/click/types.py",),
        "code_patch": "diff --git a/src/click/types.py",
    }
    return PrescreenInput(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_prompt_carries_the_issue_and_the_patch() -> None:
    content = make_input().messages()[1]["content"]
    assert "Path 类型没有去掉首尾空格" in content
    assert "复现步骤见下" in content
    assert "src/click/types.py" in content


def test_the_prompt_survives_an_empty_issue_body() -> None:
    """正文为空的候选照样要能打分（它该得 0 分），不能在拼 prompt 时就崩掉。"""
    content = make_input(body="", title="").messages()[1]["content"]
    assert "（正文为空）" in content
    assert "（无标题）" in content


def test_curly_braces_in_the_issue_do_not_break_formatting() -> None:
    """issue 正文里带 `{}` 是常事（JSON、f-string、字典）。

    prompt 是用 `str.format` 拼的，正文里的花括号如果被当成占位符，
    要么 KeyError 崩掉，要么悄悄吞掉一段内容。
    """
    body = 'JSON 是 {"key": {"nested": 1}} 这样的'
    content = make_input(body=body).messages()[1]["content"]
    assert body in content


def test_the_prompt_version_is_in_the_cache_key() -> None:
    """改了 prompt 但忘了升版本，重跑会读到旧回答 —— 而且不报错。

    那会让"我改进了 prompt"这件事完全测不出来。
    """
    salt = make_input().cache_salt()
    assert salt["prompt_version"] == PROMPT_VERSION
    assert salt["pr"] == 2946


def test_a_very_long_issue_keeps_its_head_and_its_tail() -> None:
    """只留开头是常见做法，但对 issue 不合适。

    结论、复现步骤、"我试了 X 也不行"这些常常在最后一段。掐掉尾巴会让模型
    看到一个残缺的问题描述，然后把"信息不全"归咎于 issue 本身。
    """
    body = "开头标记" + "填" * (MAX_ISSUE_CHARS * 2) + "结尾标记"
    content = make_input(body=body).messages()[1]["content"]
    assert "开头标记" in content
    assert "结尾标记" in content
    assert "中间省略" in content


# ══════════════════════════════════════════════════════════════
# 分数分布（AC：预筛分数分布合理）
# ══════════════════════════════════════════════════════════════


def test_histogram_has_one_row_per_score() -> None:
    """按整数分档而不是等宽分箱：3 分和 4 分的处置完全不同
    （人工复核 vs 直接通过），混在一格里就看不出来了。
    """
    rows = score_histogram([5, 5, 4, 3, 0])
    assert [label for label, _ in rows] == [f"{n} 分" for n in range(6)]
    assert dict(rows)["5 分"] == 2
    assert dict(rows)["1 分"] == 0


def test_histogram_is_empty_but_well_formed_with_no_scores() -> None:
    assert sum(count for _, count in score_histogram([])) == 0
