"""E3-T9（#96）：一次失败该记在谁头上 —— 外部服务和平台自己的锅不能算到被测 AI 头上。

发现经过：E6-T1 翻库里 95 次 `AGENT_RUNTIME_ERROR` 的日志，没有一次是 AI 的问题：
87 次 DeepSeek 余额不足，8 次容器一个字节没输出就被 SIGKILL（孤儿回收误杀）。
按 C-18 这两种各有自己的格子（`AGENT_AUTH_ERROR` / `SANDBOX_ERROR`），都计入平台故障率；
记成 `AGENT_RUNTIME_ERROR` 则不计入，于是一次因为欠费而全军覆没的实验能正大光明进排行榜。

这里从两个真实适配器的 `_error_for()` 一路测到 `task_run` 的映射表，
三层缺一不可：判据认得出、适配器用了它、评测单元翻译对了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.enums import InfraOutcome
from app.domain.protocol import INFRA_TO_AGENT_MAPPING, FaultOwner, InfraFailureCounting
from app.evaluation.task_run import _AGENT_ERROR_TO_INFRA, _outcome_for_agent_error
from app.runner.adapters import aider, claude_code
from app.runner.adapters.cli_text import (
    LITELLM_EXTERNAL_ERRORS,
    killed_silently,
    looks_like_auth_failure,
    looks_like_external_service_failure,
    shared_failure,
)
from app.runner.protocol import (
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    EXTERNAL_SERVICE_ERROR,
    OOM_KILLED,
    RUNTIME_ERROR,
    SANDBOX_KILLED,
    AgentError,
    AgentRunResult,
)
from app.sandbox.container import SIGKILL_EXIT_CODE, ContainerResult

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cli_text"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _container(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    oom_killed: bool = False,
    timed_out: bool = False,
) -> ContainerResult:
    return ContainerResult(
        container_id="deadbeef0000",
        image="bench-agent:test",
        exit_code=exit_code,
        oom_killed=oom_killed,
        timed_out=timed_out,
        duration_s=6.0,
        stdout=stdout,
        stderr=stderr,
    )


def _infra(error: AgentError) -> InfraOutcome:
    """适配器报的错 → 评测单元记的 `infra_outcome`，走真正的那张表。"""
    now = datetime.now(UTC)
    result = AgentRunResult(
        agent_name="x",
        agent_version="0",
        model="m",
        started_at=now,
        finished_at=now,
        duration_ms=1,
        exit_code=1,
        patch="",
        patch_source="git_diff",
        cost_source="unavailable",
        error=error,
    )
    return _outcome_for_agent_error(result)


# ── 下面几段"外部服务失败"的原文是**按 litellm / Claude Code 打出来的格式写的**，不是抓回来的 ──
# 本机 1931 份真实 agent 日志里一次限流 / 5xx 都没发生过（2026-09-17 查过），
# 所以拿不到真品。余额不足和 401 那几份是真的，在 tests/fixtures/cli_text/。

# aider 把 litellm 的异常打在 stdout，而且照样退 0（和余额不足那份一样）
AIDER_RATE_LIMIT = (
    "Aider v0.86.2\nModel: deepseek/deepseek-chat with diff edit format\n"
    "litellm.RateLimitError: RateLimitError: DeepseekException - "
    '{"error":{"message":"Rate limit reached","type":"rate_limit_error","param":null,'
    '"code":"rate_limit_exceeded"}}\n'
)
# aider 按终端宽度硬折行，会从单词中间劈开（`squash()` 的注释里有真例）
AIDER_RATE_LIMIT_WRAPPED = AIDER_RATE_LIMIT.replace(
    "litellm.RateLimitError", "litellm.RateLimit\nError"
)
AIDER_503 = (
    "litellm.ServiceUnavailableError: ServiceUnavailableError: DeepseekException - "
    '{"error":{"message":"Service Unavailable","type":"server_error"}}\n'
)
AIDER_500 = "litellm.InternalServerError: InternalServerError: DeepseekException - upstream error\n"
AIDER_CONNECTION = (
    "litellm.APIConnectionError: APIConnectionError: DeepseekException - Connection error.\n"
)


def _claude_stream(text: str) -> str:
    """Claude Code 的 stream-json 里 API 报错长什么样 —— 照 2026-09-12 那 44 次 402 的真实形态：
    先是一条 `assistant` 事件，`text` 字段整个就是报错；最后 `result` 事件再带一遍。
    错误体里的引号在 JSON 里是转义过的，判据必须在这种形态上能认。"""
    assistant = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    result = {"type": "result", "subtype": "success", "is_error": True, "result": text}
    return json.dumps(assistant) + "\n" + json.dumps(result) + "\n"


# Claude Code 走 Anthropic 兼容端点，报的是 `API Error: <状态码> <错误体>`
CLAUDE_429 = _claude_stream(
    'API Error: 429 {"type":"error","error":{"type":"rate_limit_error",'
    '"message":"This request would exceed your organization\'s rate limit"}}'
)
CLAUDE_529 = _claude_stream(
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
)
CLAUDE_500 = _claude_stream(
    'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
)
# openai SDK 风格（有的中转端点这么报）
OPENAI_503 = (
    "Error code: 503 - {'error': {'message': 'Service Unavailable', 'type': 'server_error'}}\n"
)


# ── 一、判据本身 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        AIDER_RATE_LIMIT,
        AIDER_RATE_LIMIT_WRAPPED,
        AIDER_503,
        AIDER_500,
        AIDER_CONNECTION,
        CLAUDE_429,
        CLAUDE_529,
        CLAUDE_500,
        OPENAI_503,
    ],
)
def test_external_service_failures_are_recognised(text: str) -> None:
    assert looks_like_external_service_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        # 被测 AI 在讨论代码。本机 1931 份真实日志里 52 份含 "429"、3 份含 "overloaded"，全是这种
        "I'll add a retry when the upstream returns 429 or 500; see line 529 of client.py.",
        "The server was overloaded so the bad gateway page rendered; rate limit is 500 items.",
        "I'll catch the rate limit error and the internal server error here.",
        "Tokens: 5429 sent, 1500 received. Cost: $0.0050 message.",
        # 一条失败的测试输出：HTTP 短语挤掉空白就是异常类名，所以类名必须带 litellm. 前缀
        "HTTP/1.1 500 Internal Server Error\nstatus=200",
        "FAILED tests/test_app.py::test_boom - assert Response(status_code=503)",
        # AI 在对话里提到 API 报错，但不是 CLI 自己打的那种整字段形态
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Last time I saw API Error: 500 in the logs I added a retry.",
                        }
                    ]
                },
            }
        ),
        "the API error 500 thing",
        # 状态码后面还有数字就不是状态码
        _claude_stream("API Error: 4290"),
        # 4xx 里只有 429 算外部服务；400 是我们请求写错了，404 是模型名配错了
        _claude_stream("API Error: 400 bad request"),
        "Error code: 404 - {'error': 'model not found'}",
    ],
)
def test_ordinary_text_is_not_an_external_service_failure(text: str) -> None:
    """裸词不进清单：AI 的对话里什么词都有。"""
    assert not looks_like_external_service_failure(text)


def test_litellm_class_names_look_like_class_names() -> None:
    """清单里写的是 litellm 的异常类名（2026-09-17 在 bench-agent:py311-aider 镜像里逐个核过）。

    后端环境不装 litellm（它只在 aider 的容器里），所以这里只能挡住手滑：
    类名是 CamelCase、以 Error 结尾、不带空格 —— 拼错一个就是一类失败永远认不出来。
    """
    for name in LITELLM_EXTERNAL_ERRORS:
        assert name[0].isupper() and name.endswith("Error") and " " not in name, name


def test_real_balance_fixture_is_auth_not_external() -> None:
    """余额不足走鉴权那条（2026-09-12 修的），不会同时被这条认走，分类稳定。"""
    text = _fixture("aider_insufficient_balance.txt")
    assert looks_like_auth_failure(text)
    assert not looks_like_external_service_failure(text)


def test_progress_bar_fixture_is_neither() -> None:
    text = _fixture("aider_progress_bar_401.txt")
    assert not looks_like_auth_failure(text)
    assert not looks_like_external_service_failure(text)


# ── 二、容器被平台杀了 ────────────────────────────────────────


def test_silent_sigkill_is_the_platforms_fault() -> None:
    """#131 那 8 次的样子：退出码 137、docker 没标 OOM、不是我们超时杀的、一个字节没输出。"""
    container = _container(exit_code=SIGKILL_EXIT_CODE)
    assert killed_silently(container)
    error = shared_failure(container)
    assert error is not None and error.code == SANDBOX_KILLED
    assert _infra(error) is InfraOutcome.SANDBOX_ERROR


def test_sigkill_with_output_keeps_the_old_verdict() -> None:
    """有输出的 137 可能是 dockerd 漏收的 OOM 通知，按 C-06/C-07 不能用退出码判，维持原判。"""
    container = _container(exit_code=SIGKILL_EXIT_CODE, stdout="Aider v0.86.2\nworking...")
    assert not killed_silently(container)
    assert shared_failure(container) is None


def test_whitespace_only_output_still_counts_as_silent() -> None:
    container = _container(exit_code=SIGKILL_EXIT_CODE, stdout="\n\n", stderr="  ")
    assert killed_silently(container)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 130, 143])
def test_silent_death_with_another_exit_code_is_not_a_kill(exit_code: int) -> None:
    assert not killed_silently(_container(exit_code=exit_code))


def test_oom_and_timeout_are_not_silent_kills() -> None:
    """OOM 和超时另有归宿（`_error_for` 先判它们），这里不能抢。"""
    assert not killed_silently(_container(exit_code=SIGKILL_EXIT_CODE, oom_killed=True))
    assert not killed_silently(_container(exit_code=SIGKILL_EXIT_CODE, timed_out=True))


# ── 三、两个真实适配器都走这一份判据（AC 3） ─────────────────


def _aider(container: ContainerResult) -> AgentError | None:
    return aider._error_for(container)


def _claude(container: ContainerResult) -> AgentError | None:
    return claude_code._error_for(container, None)


@pytest.mark.parametrize("error_for", [_aider, _claude], ids=["aider", "claude-code"])
def test_real_insufficient_balance_lands_on_auth_error(error_for) -> None:  # type: ignore[no-untyped-def]
    """AC 1：喂一份真实的余额不足原文，断言一路映射到 `AGENT_AUTH_ERROR`。

    aider 那份是 2026-09-12 抓的（退出码 0！），Claude Code 那份是 `API Error: 402`。
    """
    for name in ("aider_insufficient_balance.txt", "claude_code_402.txt"):
        error = error_for(_container(exit_code=1, stdout=_fixture(name)))
        assert error is not None and error.code == AUTH_FAILED, name
        assert _infra(error) is InfraOutcome.AGENT_AUTH_ERROR


def test_aider_balance_failure_with_exit_code_zero() -> None:
    """aider 打完 litellm 的报错照样退 0，这条路 2026-09-05 就踩过，这里锁住。"""
    error = _aider(_container(exit_code=0, stdout=_fixture("aider_insufficient_balance.txt")))
    assert error is not None and error.code == AUTH_FAILED


@pytest.mark.parametrize(
    ("error_for", "text"),
    [
        (_aider, AIDER_RATE_LIMIT),
        (_aider, AIDER_RATE_LIMIT_WRAPPED),
        (_aider, AIDER_503),
        (_aider, AIDER_CONNECTION),
        (_claude, CLAUDE_429),
        (_claude, CLAUDE_529),
        (_claude, CLAUDE_500),
    ],
)
def test_external_service_failures_land_on_auth_error(error_for, text: str) -> None:  # type: ignore[no-untyped-def]
    """限流 / 5xx / 连不上 → `external_service_error` → `AGENT_AUTH_ERROR`（归属 EXTERNAL）。"""
    error = error_for(_container(exit_code=1, stdout=text))
    assert error is not None and error.code == EXTERNAL_SERVICE_ERROR
    assert _infra(error) is InfraOutcome.AGENT_AUTH_ERROR


def test_aider_rate_limit_with_exit_code_zero() -> None:
    """限流时 aider 同样退 0（litellm 异常打在 stdout 上），不能被当成"正常跑完、空补丁"。"""
    error = _aider(_container(exit_code=0, stdout=AIDER_RATE_LIMIT))
    assert error is not None and error.code == EXTERNAL_SERVICE_ERROR


@pytest.mark.parametrize("error_for", [_aider, _claude], ids=["aider", "claude-code"])
def test_silent_sigkill_lands_on_sandbox_error(error_for) -> None:  # type: ignore[no-untyped-def]
    """AC 2：容器无输出 + 137 → `sandbox_killed` → `SANDBOX_ERROR`，两个适配器一样。"""
    error = error_for(_container(exit_code=SIGKILL_EXIT_CODE))
    assert error is not None and error.code == SANDBOX_KILLED
    assert _infra(error) is InfraOutcome.SANDBOX_ERROR


@pytest.mark.parametrize("error_for", [_aider, _claude], ids=["aider", "claude-code"])
def test_oom_still_wins_over_everything(error_for) -> None:  # type: ignore[no-untyped-def]
    """顺序不能换：OOM 和超时先判（两者退出码也是 137）。"""
    oom = error_for(_container(exit_code=SIGKILL_EXIT_CODE, oom_killed=True))
    assert oom is not None and oom.code == OOM_KILLED
    timeout = error_for(_container(exit_code=SIGKILL_EXIT_CODE, timed_out=True))
    assert timeout is not None and timeout.code == DEADLINE_EXCEEDED


@pytest.mark.parametrize("error_for", [_aider, _claude], ids=["aider", "claude-code"])
def test_the_agents_own_crash_is_still_its_own(error_for) -> None:  # type: ignore[no-untyped-def]
    """AI 自己崩了（有输出、退出码 1、没有外部服务的痕迹）还是 `runtime_error`。

    这一条是反方向的保险：判据放宽过头，会把 AI 的失败判给外部服务、白重试 3 次。
    """
    stdout = "Traceback (most recent call last):\n  File 'x.py', line 500\nKeyError: 'rate limit'\n"
    error = error_for(_container(exit_code=1, stdout=stdout))
    assert error is not None and error.code == RUNTIME_ERROR
    assert _infra(error) is InfraOutcome.AGENT_RUNTIME_ERROR


def test_aider_normal_run_with_scary_words_is_not_an_error() -> None:
    """退 0、没有 litellm 异常 —— 哪怕对话里满是 429 和 overloaded，也是正常跑完。"""
    stdout = "Aider v0.86.2\nI'll handle 429 and overloaded responses.\nApplied edit to client.py\n"
    assert _aider(_container(exit_code=0, stdout=stdout)) is None


# ── 四、映射表和协议对得上 ────────────────────────────────────


def test_every_canonical_code_has_a_row_in_the_table() -> None:
    """认不出来的码一律当 `AGENT_RUNTIME_ERROR`，所以漏一行不会报错 —— 只会静悄悄记错账。"""
    for code in (
        DEADLINE_EXCEEDED,
        AUTH_FAILED,
        RUNTIME_ERROR,
        OOM_KILLED,
        EXTERNAL_SERVICE_ERROR,
        SANDBOX_KILLED,
    ):
        assert code in _AGENT_ERROR_TO_INFRA, code


def test_the_two_new_rows_count_as_infra_failures() -> None:
    """E3-T9 的意义就在这一行：这两种失败按 C-18 计入平台故障率，C-26 的门槛才拦得住。"""
    for code, owner in (
        (EXTERNAL_SERVICE_ERROR, FaultOwner.EXTERNAL),
        (SANDBOX_KILLED, FaultOwner.PLATFORM),
    ):
        rule = INFRA_TO_AGENT_MAPPING[_AGENT_ERROR_TO_INFRA[code]]
        assert rule.owner is owner
        assert rule.counts_as_infra_failure is InfraFailureCounting.YES
    old = INFRA_TO_AGENT_MAPPING[_AGENT_ERROR_TO_INFRA[RUNTIME_ERROR]]
    assert old.counts_as_infra_failure is InfraFailureCounting.NO, (
        "对照：记成 runtime_error 就不计入"
    )
