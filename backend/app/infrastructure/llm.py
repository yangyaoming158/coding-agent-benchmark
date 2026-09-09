"""调大模型的最小客户端（E1-T5 起用，E6-T2 归因也要用）。

一句话：**把"问一次模型"变成一个会缓存、会重试、会记账的函数。**

## 为什么放 `infrastructure` 而不是 `benchmark`

两个地方要它：E1-T5 的候选预筛（`app.benchmark`）、E6-T2 的失败归因
（`app.attribution`）。而 import-linter 的分层里 `app.attribution` 在
`app.benchmark` **下面**一层 —— 放 benchmark 里的话 attribution 根本 import 不到，
最后只能再抄一份。放 `infrastructure` 两边都够得着。

## 为什么不装 SDK

`httpx` 早就是依赖了，而我们要的只有一个 `POST /chat/completions`。
DeepSeek、OpenAI、DashScope、月之暗面都认这套接口，换一家只改 `base_url` 和模型名。
装 `openai` 或 `anthropic` 换来的是"多两个包要跟着升级"，以及一套我们只用 3%
的抽象 —— 而真正难的部分（缓存、重试、记账）它们都不管。

## 三件它必须做对的事

1. **缓存**。预筛 80 条候选、归因几百条失败，重跑一次就是重新付一次钱。
   缓存 key 里放**所有会影响回答的东西**（模型、消息、温度、prompt 版本），
   漏一个就会"改了 prompt 还读到旧答案"，而且不报错。
2. **温度默认 0**。判定相关的用途（预筛打分、归因）要的是可复现，不是文采。
   同一条候选今天打 4 分、下个月打 2 分，那这个分数就没法当门槛用。
3. **不把 Key 写进任何输出**。异常消息里带 URL 和状态码，不带 header。

## 它不做的事

不做重排、不做多路投票、不做函数调用。E6-T2 要的"低置信投票"是那一层自己的事 ——
它拿 `complete()` 调三次就行，没必要塞进客户端。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.infrastructure import file_cache
from app.infrastructure.config import PROVIDER_KEY_ENV, Settings, get_settings
from app.infrastructure.file_cache import CACHE_ROOT, DEFAULT_TTL_S, CacheStats
from app.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: 大模型回答的缓存命名空间。和 GitHub 的分开：清掉 GitHub 缓存重拉一遍时，
#: 不该把已经花过钱的回答一起删掉。
NAMESPACE = "llm"

#: 模型名里出现哪个词 → 该打哪个地址。和 `PROVIDER_KEY_ENV` 一样用包含匹配，
#: 顺序有意义，具体的写在前面。
PROVIDER_BASE_URL: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "openai": "https://api.openai.com/v1",
    "gpt": "https://api.openai.com/v1",
}

#: 单次请求的墙钟上限。给到 180 秒是因为长 prompt 上模型要想一会儿，
#: 而重试一次的代价（又一次完整生成）比多等 60 秒贵。
DEFAULT_TIMEOUT_S = 180.0

#: 重试次数和退避（秒）。429 和 5xx 才重试 —— 400（prompt 写错了）重试多少次都一样。
MAX_RETRIES = 3
RETRY_BACKOFF_S = (2.0, 8.0, 20.0)

#: 默认温度。**判定相关的用途必须是 0**，理由见模块文档。
DEFAULT_TEMPERATURE = 0.0


class LLMError(RuntimeError):
    """调模型失败，且不是可重试的那几种。"""


class LLMConfigError(LLMError):
    """没配模型或没配 Key。这不是故障，是"这台机器还没准备好"。"""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次回答。`cached=True` 表示这次没花钱。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def json_payload(self) -> Any:
        """把回答当 JSON 解析。模型偶尔会在 JSON 外面裹一层 ```json 围栏，这里剥掉。

        剥不出来就抛 `LLMError`，**不返回一个空 dict** —— 静默吞掉解析失败会让
        "模型没按格式答"长得和"模型说它没意见"一模一样。
        """
        raw = self.text.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型没返回合法 JSON：{exc}；原文前 200 字：{self.text[:200]}") from exc


@dataclass
class UsageLedger:
    """一轮作业累计问了多少次、烧了多少 token。CLI 打"这次花了多少"用。"""

    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, response: LLMResponse) -> None:
        self.calls += 1
        if response.cached:
            self.cached_calls += 1
            return
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens

    @property
    def billed_calls(self) -> int:
        return self.calls - self.cached_calls

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "billed_calls": self.billed_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def resolve_api_key(model: str, settings: Settings | None = None) -> str | None:
    """按模型名挑该用哪家的 Key。认不出来就返回 `None`，不瞎猜。

    `judge_api_key` 优先：它是"就用这一把"的显式覆盖，配了就说明有意为之
    （比如走中转站，模型名是 `gpt-4o` 但 Key 不是 OpenAI 的）。
    """
    s = settings or get_settings()
    if s.judge_api_key is not None:
        return s.judge_api_key.get_secret_value()
    lowered = model.lower()
    for marker, var in PROVIDER_KEY_ENV.items():
        if marker in lowered:
            secret = getattr(s, var.lower(), None)
            return secret.get_secret_value() if secret is not None else None
    return None


def resolve_base_url(model: str, settings: Settings | None = None) -> str | None:
    """按模型名挑地址。`judge_base_url` 配了就用它（中转站、私有部署）。"""
    s = settings or get_settings()
    if s.judge_base_url:
        return s.judge_base_url.rstrip("/")
    lowered = model.lower()
    for marker, url in PROVIDER_BASE_URL.items():
        if marker in lowered:
            return url
    return None


def strip_provider(model: str) -> str:
    """`deepseek/deepseek-chat` → `deepseek-chat`。

    litellm 风格的 `厂商/模型` 写法在配置里更好认（一眼看出打谁家），
    但 OpenAI 兼容接口要的是**裸模型名**，带斜杠会 404。
    """
    return model.split("/", 1)[1] if "/" in model else model


@dataclass
class LLMClient:
    """一个 OpenAI 兼容接口的客户端。

    刻意做成"建一次、问很多次"：`httpx.Client` 复用连接，
    预筛 80 条候选就是 80 次请求，每次重新握手很浪费。
    """

    model: str
    api_key: str
    base_url: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    ttl_s: int = DEFAULT_TTL_S
    cache_root: Path = CACHE_ROOT
    #: 走不走代理。`None` = 直连。见 `Settings.llm_http_proxy` 的说明。
    proxy: str | None = None
    stats: CacheStats = field(default_factory=CacheStats)
    usage: UsageLedger = field(default_factory=UsageLedger)
    _client: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> LLMClient:
        """从配置建客户端。缺模型或缺 Key 时抛 `LLMConfigError`，消息里说清缺哪个。"""
        s = settings or get_settings()
        chosen = model or s.judge_model
        if not chosen:
            raise LLMConfigError(
                "没配模型。在 .env 里设 JUDGE_MODEL（比如 deepseek/deepseek-chat），"
                "或者用命令行的 --model 指定"
            )
        key = resolve_api_key(chosen, s)
        if not key:
            want = next(
                (v for m, v in PROVIDER_KEY_ENV.items() if m in chosen.lower()),
                "对应厂商的 API Key",
            )
            raise LLMConfigError(f"模型 {chosen} 认不到 Key。在 .env 里设 {want}")
        url = resolve_base_url(chosen, s)
        if not url:
            raise LLMConfigError(f"不知道模型 {chosen} 该打哪个地址。在 .env 里设 JUDGE_BASE_URL")
        return cls(
            model=chosen,
            api_key=key,
            base_url=url,
            ttl_s=ttl_s,
            proxy=s.llm_http_proxy,
        )

    # ── 内部 ──

    def _http(self) -> httpx.Client:
        """建 httpx 客户端。**`trust_env=False`，不吃 shell 里的代理变量。**

        默认的 `trust_env=True` 会读 `HTTP_PROXY` / `ALL_PROXY`。两个问题：

        1. **国产模型被绕去境外代理。** DeepSeek 在国内，绕一圈更慢更不稳。
        2. **这台机器的 `ALL_PROXY` 是 `socks5h://`**，httpx 认它要装 `socksio`，
           没装就在**建客户端时**直接 ImportError —— 错误信息只说"缺个包"，
           看不出是环境变量带进来的（2026-09-09 实测）。

        要走代理就在 `.env` 里显式设 `LLM_HTTP_PROXY`，让它成为一个可见的决定。
        """
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s, trust_env=False, proxy=self.proxy)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """发一次请求，429 / 5xx 退避重试。

        **异常消息里不带 header**：Key 就在 header 里，而异常会进日志、
        进报表、进 issue。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last = ""
        for attempt in range(MAX_RETRIES + 1):
            try:
                reply = self._http().post(url, headers=headers, json=dict(body))
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if reply.status_code == 200:
                    payload: dict[str, Any] = reply.json()
                    return payload
                last = f"HTTP {reply.status_code}：{reply.text[:300]}"
                # 4xx 里只有 429 值得重试。400/401/404 是我们自己配错了，
                # 重试三次只是把同一个错误报慢三次
                if reply.status_code != 429 and reply.status_code < 500:
                    break
            if attempt >= MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
            logger.warning(
                "调模型失败，退避重试", wait_s=wait, attempt=attempt + 1, error=last[:200]
            )
            time.sleep(wait)
        raise LLMError(f"调 {self.model} 失败：{last}")

    # ── 对外 ──

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
        json_mode: bool = False,
        cache_salt: Mapping[str, Any] | None = None,
        refresh: bool = False,
    ) -> LLMResponse:
        """问一次。

        `cache_salt` 放**不进请求体、但会影响我们怎么解读回答**的东西，
        典型的就是 prompt 版本号：prompt 改了但消息碰巧一字未变时，
        没有这一项就会读到旧答案。
        """
        body: dict[str, Any] = {
            "model": strip_provider(self.model),
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        if self.ttl_s < 0:
            return self._call(body, cached=False)

        key = file_cache.cache_key("llm", {"body": body, "salt": dict(cache_salt or {})})
        if not refresh:
            hit = file_cache.read(key, root=self.cache_root, namespace=NAMESPACE, ttl_s=self.ttl_s)
            if hit is not None:
                self.stats.hits += 1
                response = LLMResponse(
                    text=str(hit.get("text", "")),
                    model=str(hit.get("model", self.model)),
                    prompt_tokens=int(hit.get("prompt_tokens", 0)),
                    completion_tokens=int(hit.get("completion_tokens", 0)),
                    cached=True,
                )
                self.usage.record(response)
                return response

        response = self._call(body, cached=False)
        file_cache.write(
            key,
            {
                "text": response.text,
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            },
            root=self.cache_root,
            namespace=NAMESPACE,
            meta={"model": self.model},
        )
        self.stats.misses += 1
        return response

    def _call(self, body: Mapping[str, Any], *, cached: bool) -> LLMResponse:
        payload = self._post(body)
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"{self.model} 返回里没有 choices：{str(payload)[:300]}")
        text = str((choices[0].get("message") or {}).get("content") or "")
        usage = payload.get("usage") or {}
        response = LLMResponse(
            text=text,
            model=str(payload.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached=cached,
        )
        self.usage.record(response)
        return response


__all__ = [
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "MAX_RETRIES",
    "NAMESPACE",
    "PROVIDER_BASE_URL",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMResponse",
    "UsageLedger",
    "resolve_api_key",
    "resolve_base_url",
    "strip_provider",
]
