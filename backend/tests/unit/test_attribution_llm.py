"""E6-T2 LLM 归因的 prompt、结构校验与低置信投票。

所有回答都是内存里的假数据，这组测试不读 Key、不连网、不花钱。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.attribution.llm import (
    AttributionInput,
    AttributionRunError,
    FailedTest,
    attribute_failure,
    parse_verdict,
    result_cache_hit,
)
from app.domain.enums import AttributionStatus, FailureCategory
from app.infrastructure.llm import LLMError, LLMResponse


class FakeClient:
    """按顺序吐假回答，并记下每次调用参数。"""

    model = "fake/judge"

    def __init__(self, payloads: Sequence[object]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
        cache_salt: Mapping[str, Any] | None = None,
        refresh: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "cache_salt": cache_salt,
                "refresh": refresh,
            }
        )
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(text=text, model=self.model)


def make_input() -> AttributionInput:
    return AttributionInput(
        task_run_id=41,
        task_id="pallets__click-123",
        issue_title="重复注册处理器",
        issue_body="重复调用 register 时只应保留一个处理器。",
        agent_patch="@@ -10,2 +10,3 @@ def register(handler):\n+    handlers.append(handler)",
        gold_summary="文件数：1\n新增行：3\n删除行：1\n文件：\n- src/click/core.py",
        failed_tests=(
            FailedTest(
                test_id="tests/test_core.py::test_duplicate",
                status="FAILED",
                message="AssertionError: expected 1 handler, got 2",
            ),
        ),
        features={
            "patch_overlap": {"hit_any": True, "jaccard": 1.0},
            "message_shift": {"changed": 1, "unchanged": 0},
            "trajectory": {"tool_calls": 3, "tool_errors": 0, "tail": []},
        },
    )


def answer(category: str = "F3_INCOMPLETE_FIX", confidence: float = 0.9) -> dict[str, object]:
    return {
        "category": category,
        "confidence": confidence,
        "evidence": [{"source": "test_log", "quote": "AssertionError: expected 1 handler, got 2"}],
        "reasoning_zh": "修改了正确位置，但重复注册仍然失败。",
        "secondary_category": None,
    }


def test_prompt_contains_required_material_but_not_gold_code() -> None:
    payload = make_input()
    prompt = payload.messages()[1]["content"]

    assert "重复调用 register" in prompt
    assert "handlers.append(handler)" in prompt
    assert "src/click/core.py" in prompt
    assert "AssertionError: expected 1 handler, got 2" in prompt
    assert "JSON Schema" in prompt
    assert not hasattr(payload, "gold_patch"), "输入类不应留官方补丁正文的口子"


def test_evidence_must_be_an_exact_quote_from_its_source() -> None:
    bad = answer()
    bad["evidence"] = [{"source": "test_log", "quote": "模型自己编的证据"}]

    with pytest.raises(ValueError, match="evidence 不在 test_log 原文"):
        parse_verdict(bad, sources=make_input().sections())


def test_llm_cannot_output_rule_or_human_categories() -> None:
    with pytest.raises(ValueError, match="F1～F5"):
        parse_verdict(
            answer("F6_REGRESSION"),
            sources=make_input().sections(),
        )


def test_extra_fields_are_rejected_by_the_schema() -> None:
    with pytest.raises(ValueError, match="JSON Schema"):
        parse_verdict(
            {**answer(), "agent_outcome": "RESOLVED"},
            sources=make_input().sections(),
        )


def test_high_confidence_needs_one_deterministic_call() -> None:
    client = FakeClient([answer(confidence=0.92)])

    result = attribute_failure(make_input(), client)

    assert result.status is AttributionStatus.OK
    assert result.verdict.category is FailureCategory.F3_INCOMPLETE_FIX
    assert len(client.calls) == 1
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["max_tokens"] == 4096
    assert client.calls[0]["json_mode"] is True
    salt = client.calls[0]["cache_salt"]
    assert isinstance(salt, Mapping)
    assert salt["task_run_id"] == 41
    assert salt["prompt_hash"] == make_input().prompt_hash()


def test_low_confidence_uses_three_votes_and_takes_the_majority() -> None:
    client = FakeClient(
        [
            answer("F3_INCOMPLETE_FIX", 0.4),
            answer("F4_INCORRECT_LOGIC", 0.8),
            answer("F3_INCOMPLETE_FIX", 0.55),
        ]
    )

    result = attribute_failure(make_input(), client)

    assert len(client.calls) == 3
    assert result.status is AttributionStatus.OK
    assert result.verdict.category is FailureCategory.F3_INCOMPLETE_FIX
    assert result.raw_response["vote_categories"] == [
        "F3_INCOMPLETE_FIX",
        "F4_INCORRECT_LOGIC",
        "F3_INCOMPLETE_FIX",
    ]


def test_three_different_low_confidence_votes_need_a_human() -> None:
    client = FakeClient(
        [
            answer("F2_WRONG_FILE_LOCALIZATION", 0.3),
            answer("F3_INCOMPLETE_FIX", 0.5),
            answer("F4_INCORRECT_LOGIC", 0.4),
        ]
    )

    result = attribute_failure(make_input(), client)

    assert result.status is AttributionStatus.NEEDS_HUMAN
    assert len(client.calls) == 3


def test_invalid_json_is_retried_then_a_valid_answer_is_used() -> None:
    client = FakeClient(["not json", answer(confidence=0.8)])

    result = attribute_failure(make_input(), client)

    assert result.status is AttributionStatus.OK
    assert len(client.calls) == 2
    first_salt = client.calls[0]["cache_salt"]
    second_salt = client.calls[1]["cache_salt"]
    assert isinstance(first_salt, Mapping)
    assert isinstance(second_salt, Mapping)
    assert first_salt["validation_attempt"] == 0
    assert second_salt["validation_attempt"] == 1
    second_messages = client.calls[1]["messages"]
    assert isinstance(second_messages, list)
    assert "逐字" in second_messages[-1]["content"]


def test_invalid_patch_quote_gets_a_changed_retry_prompt() -> None:
    bad = answer()
    bad["evidence"] = [{"source": "patch", "quote": "handlers.append(handler) without diff prefix"}]
    client = FakeClient([bad, answer()])

    result = attribute_failure(make_input(), client)

    assert result.status is AttributionStatus.OK
    assert len(client.calls) == 2
    first_messages = client.calls[0]["messages"]
    second_messages = client.calls[1]["messages"]
    assert isinstance(first_messages, list)
    assert isinstance(second_messages, list)
    assert len(second_messages) == len(first_messages) + 1
    assert "不要引用 patch" in second_messages[-1]["content"]


def test_three_invalid_answers_fail_without_inventing_a_category() -> None:
    client = FakeClient(["bad 1", "bad 2", "bad 3"])

    with pytest.raises(AttributionRunError, match="没有可用"):
        attribute_failure(make_input(), client)

    assert len(client.calls) == 3


def test_transport_failure_is_not_multiplied_after_client_retries() -> None:
    """LLMClient 自己已经退避 3 次；上层不能再整体重试 3 遍。"""
    client = FakeClient([LLMError("HTTP 503 after retries")])

    with pytest.raises(AttributionRunError, match="HTTP 503"):
        attribute_failure(make_input(), client)

    assert len(client.calls) == 1


def test_prompt_hash_changes_with_the_material() -> None:
    first = make_input()
    second = AttributionInput(
        task_run_id=first.task_run_id,
        task_id=first.task_id,
        issue_title=first.issue_title,
        issue_body=first.issue_body + "新的复现信息",
        agent_patch=first.agent_patch,
        gold_summary=first.gold_summary,
        failed_tests=first.failed_tests,
        features=first.features,
    )

    assert first.prompt_hash() != second.prompt_hash()


def test_database_cache_identity_requires_both_prompt_and_model() -> None:
    prompt_hash = "a" * 64

    assert result_cache_hit("judge-v1", prompt_hash, model="judge-v1", prompt_hash=prompt_hash)
    assert not result_cache_hit("judge-v1", prompt_hash, model="judge-v2", prompt_hash=prompt_hash)
    assert not result_cache_hit("judge-v1", prompt_hash, model="judge-v1", prompt_hash="b" * 64)
