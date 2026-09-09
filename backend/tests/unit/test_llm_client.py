"""调大模型的客户端（E1-T5 起用，E6-T2 归因也要用）。

**这一组不联网、不花钱**：`httpx` 那一层换成假的。

三件事必须验到，因为错了都要花真钱：

1. **缓存真的挡住了第二次请求。** 挡不住的话，预筛重跑一次就是重新付一次钱，
   而且账单上看不出是"重跑"还是"新活"。
2. **该重试的重试、不该重试的立刻失败。** 400（prompt 写错了）重试三次
   只是把同一个错误报慢三次，还各付一次钱。
3. **异常消息里不带 Key。** 异常会进日志、进报表、进 issue。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.infrastructure import llm
from app.infrastructure.llm import LLMClient, LLMConfigError, LLMError, LLMResponse

FAKE_KEY = "sk-" + "test" + "0" * 20


def ok_payload(text: str = '{"score": 5}') -> dict[str, Any]:
    return {
        "model": "deepseek-chat",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def client(tmp_path: Path, **overrides: Any) -> LLMClient:
    return LLMClient(
        model="deepseek/deepseek-chat",
        api_key=FAKE_KEY,
        base_url="https://api.deepseek.com",
        cache_root=tmp_path,
        **overrides,
    )


def fake_posts(monkeypatch: pytest.MonkeyPatch, results: list[Any]) -> list[dict[str, Any]]:
    """按顺序返回预设结果，并记下每次的请求体。结果是异常就抛出来。"""
    seen: list[dict[str, Any]] = []

    def post(self: Any, url: str, *, headers: Any = None, json: Any = None) -> Any:
        seen.append({"url": url, "headers": dict(headers or {}), "body": json})
        outcome = results[min(len(seen) - 1, len(results) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    return seen


# ══════════════════════════════════════════════════════════════
# 挑 Key 和地址
# ══════════════════════════════════════════════════════════════


def test_the_provider_is_recognised_from_the_model_name() -> None:
    assert llm.resolve_base_url("deepseek/deepseek-chat") == "https://api.deepseek.com"
    assert llm.resolve_base_url("gpt-4o") == "https://api.openai.com/v1"


def test_an_unknown_model_gives_no_url_instead_of_guessing() -> None:
    """认不出来就说认不出来。瞎猜一个地址会打到别人家去。"""
    assert llm.resolve_base_url("某个自研模型") is None


def test_the_provider_prefix_is_stripped_before_sending() -> None:
    """litellm 风格的 `厂商/模型` 在配置里好认，但 OpenAI 兼容接口要裸模型名，
    带斜杠会 404。
    """
    assert llm.strip_provider("deepseek/deepseek-chat") == "deepseek-chat"
    assert llm.strip_provider("gpt-4o") == "gpt-4o"


def test_a_missing_model_says_which_setting_to_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺配置不是故障，是"这台机器还没准备好"。消息里要说清缺哪个。"""
    from app.infrastructure.config import Settings

    with pytest.raises(LLMConfigError, match="JUDGE_MODEL"):
        LLMClient.from_settings(Settings(judge_model=None))


def test_a_model_without_a_key_says_which_env_var_to_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.infrastructure.config import Settings

    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        LLMClient.from_settings(Settings(judge_model="gpt-4o", openai_api_key=None))


# ══════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════


def test_the_second_identical_question_does_not_hit_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**挡不住的话，预筛重跑一次就是重新付一次钱。**"""
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    c = client(tmp_path)
    messages = [{"role": "user", "content": "问题"}]

    first = c.complete(messages)
    second = c.complete(messages)

    assert len(calls) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.text == first.text


def test_a_cached_answer_is_not_counted_as_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中缓存的那次不能算进 token 账，否则"这轮花了多少"永远偏高。"""
    fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    c = client(tmp_path)
    messages = [{"role": "user", "content": "问题"}]
    c.complete(messages)
    c.complete(messages)

    assert c.usage.calls == 2
    assert c.usage.cached_calls == 1
    assert c.usage.billed_calls == 1
    assert c.usage.prompt_tokens == 100, "只算真发出去那一次"


def test_changing_the_prompt_version_misses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改了 prompt 但读到旧回答，会让"我改进了 prompt"完全测不出来。"""
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    c = client(tmp_path)
    messages = [{"role": "user", "content": "问题"}]
    c.complete(messages, cache_salt={"prompt_version": "1.0"})
    c.complete(messages, cache_salt={"prompt_version": "1.1"})
    assert len(calls) == 2


def test_changing_the_temperature_misses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """温度进请求体，所以自动进缓存 key。不进的话换了温度还读到旧答案。"""
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    c = client(tmp_path)
    messages = [{"role": "user", "content": "问题"}]
    c.complete(messages, temperature=0.0)
    c.complete(messages, temperature=0.7)
    assert len(calls) == 2


def test_refresh_bypasses_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    c = client(tmp_path)
    messages = [{"role": "user", "content": "问题"}]
    c.complete(messages)
    c.complete(messages, refresh=True)
    assert len(calls) == 2


def test_temperature_defaults_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一条候选今天打 4 分、下个月打 2 分的话，这个分数就没法当门槛用。"""
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    client(tmp_path).complete([{"role": "user", "content": "问题"}])
    assert calls[0]["body"]["temperature"] == 0.0


# ══════════════════════════════════════════════════════════════
# 重试
# ══════════════════════════════════════════════════════════════


def test_rate_limiting_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(
        monkeypatch, [FakeResponse(429, text="too many"), FakeResponse(200, ok_payload())]
    )
    assert client(tmp_path).complete([{"role": "user", "content": "x"}]).text == '{"score": 5}'
    assert len(calls) == 2


def test_a_server_error_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(
        monkeypatch, [FakeResponse(503, text="down"), FakeResponse(200, ok_payload())]
    )
    client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert len(calls) == 2


def test_a_network_blip_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """这台机器过代理很不稳（`AGENTS.md` §10）。"""
    calls = fake_posts(
        monkeypatch, [httpx.ConnectError("connection reset"), FakeResponse(200, ok_payload())]
    )
    client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert len(calls) == 2


def test_a_bad_request_fails_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """400 是我们自己配错了。重试三次只是把同一个错误报慢三次，还各付一次钱。"""
    calls = fake_posts(monkeypatch, [FakeResponse(400, text="bad model name")])
    with pytest.raises(LLMError, match="bad model name"):
        client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert len(calls) == 1


def test_an_auth_error_fails_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(monkeypatch, [FakeResponse(401, text="invalid api key")])
    with pytest.raises(LLMError):
        client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert len(calls) == 1


def test_retrying_gives_up_after_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(monkeypatch, [FakeResponse(503, text="down")])
    with pytest.raises(LLMError):
        client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert len(calls) == llm.MAX_RETRIES + 1


def test_a_failed_call_writes_nothing_to_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把失败缓存下来的话，重跑会一直读到那个失败，而且不再发请求。"""
    fake_posts(monkeypatch, [FakeResponse(500, text="boom")])
    with pytest.raises(LLMError):
        client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert list(tmp_path.rglob("*.json")) == []


# ══════════════════════════════════════════════════════════════
# 密钥不能漏出去
# ══════════════════════════════════════════════════════════════


def test_the_api_key_is_never_in_the_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常会进日志、进报表、进 issue。Key 在 header 里，不能跟着冒出来。"""
    fake_posts(monkeypatch, [FakeResponse(401, text="invalid api key")])
    with pytest.raises(LLMError) as caught:
        client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert FAKE_KEY not in str(caught.value)


def test_the_api_key_goes_in_the_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    client(tmp_path).complete([{"role": "user", "content": "x"}])
    assert calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


# ══════════════════════════════════════════════════════════════
# 解析回答
# ══════════════════════════════════════════════════════════════


def test_json_wrapped_in_a_code_fence_is_unwrapped() -> None:
    """让模型输出 JSON，它还是常常裹一层 ```json 围栏。"""
    response = LLMResponse(text='```json\n{"score": 4}\n```', model="x")
    assert response.json_payload() == {"score": 4}


def test_plain_json_parses() -> None:
    assert LLMResponse(text='{"score": 4}', model="x").json_payload() == {"score": 4}


def test_a_non_json_answer_raises_instead_of_returning_empty() -> None:
    """静默吞掉解析失败会让"模型没按格式答"长得和"模型说它没意见"一模一样。"""
    with pytest.raises(LLMError, match="合法 JSON"):
        LLMResponse(text="我觉得这题还行", model="x").json_payload()


def test_json_mode_is_requested_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_posts(monkeypatch, [FakeResponse(200, ok_payload())])
    client(tmp_path).complete([{"role": "user", "content": "x"}], json_mode=True)
    assert calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_an_empty_choices_list_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内容审核拦下时 `choices` 会是空的。当成空回答会让它长得像"模型无话可说"。"""
    fake_posts(monkeypatch, [FakeResponse(200, {"choices": []})])
    with pytest.raises(LLMError, match="没有 choices"):
        client(tmp_path).complete([{"role": "user", "content": "x"}])
