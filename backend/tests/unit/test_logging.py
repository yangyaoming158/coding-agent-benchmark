"""结构化日志的测试（E0-T4 的两条验收标准）。

一条是"日志带结构化上下文"：`run_id` / `task_run_id` 要自动跟着，
不能靠每个函数手动传参 —— 靠手动传，总有地方会漏。

另一条是"敏感值在日志中脱敏"。这条测得比较细，因为它是**唯一**一道运行期防线：
密钥一旦打进日志文件，删日志已经晚了。三种漏法各有对应的用例：
字段名带 key、被测 AI 自己把 Key 回显在 stdout 里、以及日志里出现了
没在配置中出现过的第三方密钥。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO
from typing import Any

import pytest
import structlog

from app.infrastructure.config import Settings
from app.infrastructure.logging import (
    MASK,
    bind_run_context,
    clear_registered_secrets,
    configure_logging,
    get_logger,
    redact_secrets,
    register_secret,
)


def _fake_secret(prefix: str, body: str) -> str:
    """拼一个假密钥出来。

    **必须拼，不能整串写在源码里**，否则 `scripts/check_secrets.py` 扫到这个
    测试文件自己就会报警，提交被钩子拦下、CI 变红 —— 这个坑
    `test_repo_guards.py` 里已经踩过一次了。

    注意 `make check` **不跑**密钥扫描（它是 pre-commit 钩子 + 独立的 CI job），
    所以本地全绿也发现不了这个问题。
    """
    return prefix + body


FAKE_ANTHROPIC = _fake_secret("sk-ant-", "api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE")
FAKE_GITHUB = _fake_secret("ghp_", "FAKEfake0123456789abcdefghijklmnop")


@pytest.fixture(autouse=True)
def isolate_logging() -> Iterator[None]:
    """日志配置是全局状态，用完必须还回去，否则会污染别的测试文件。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    structlog.reset_defaults()
    clear_registered_secrets()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def setup_json_logging(stream: StringIO, **overrides: Any) -> None:
    """把日志接到内存流上，用 JSON 格式 —— 断言渲染后的真实输出，而不是只测处理器函数。"""
    settings = Settings(_env_file=None, log_format="json", **overrides)
    configure_logging(settings, stream=stream)


def emitted(stream: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# ══════════════════════════════════════════════════════════
# 一、结构化上下文
# ══════════════════════════════════════════════════════════


def test_event_has_structured_fields() -> None:
    """日志是一组字段，不是一句话 —— 这样才能按 task_run_id 过滤出一道题的全过程。"""
    stream = StringIO()
    setup_json_logging(stream)
    get_logger("test").info("容器已退出", exit_code=137, oom_killed=True)

    (event,) = emitted(stream)
    assert event["event"] == "容器已退出"
    assert event["exit_code"] == 137
    assert event["oom_killed"] is True
    assert event["level"] == "info"
    assert "timestamp" in event


def test_run_context_is_attached_automatically() -> None:
    """绑一次 run_id/task_run_id，块里所有日志自动带上。"""
    stream = StringIO()
    setup_json_logging(stream)
    log = get_logger("test")

    with bind_run_context(run_id=12, task_run_id=340):
        log.info("开始物化工作区")
        log.info("补丁已捕获", files_changed=3)

    events = emitted(stream)
    assert len(events) == 2
    for event in events:
        assert event["run_id"] == 12
        assert event["task_run_id"] == 340
    assert events[1]["files_changed"] == 3


def test_run_context_nests_and_restores() -> None:
    """外层绑实验、内层绑单题，退出内层要还原回去。

    编排器就是这么用的：一次实验里循环跑几百道题，`run_id` 绑一次，
    每道题再绑自己的 `task_run_id`。内层不还原的话，第二道题会带着第一道题的 ID。
    """
    stream = StringIO()
    setup_json_logging(stream)
    log = get_logger("test")

    with bind_run_context(run_id=12):
        with bind_run_context(task_run_id=340):
            log.info("题目开始")
        log.info("题目结束")

    inner, outer = emitted(stream)
    assert (inner["run_id"], inner["task_run_id"]) == (12, 340)
    assert outer["run_id"] == 12
    assert "task_run_id" not in outer


def test_context_gone_after_block() -> None:
    stream = StringIO()
    setup_json_logging(stream)
    log = get_logger("test")

    with bind_run_context(run_id=12):
        log.info("块内")
    log.info("块外")

    inside, outside = emitted(stream)
    assert inside["run_id"] == 12
    assert "run_id" not in outside


def test_none_context_fields_are_dropped() -> None:
    """没传的字段不要打成 null，那是噪声，还会让日志检索多一堆空值。"""
    stream = StringIO()
    setup_json_logging(stream)

    with bind_run_context(run_id=12):
        get_logger("test").info("只有 run_id")

    (event,) = emitted(stream)
    assert event["run_id"] == 12
    for absent in ("task_run_id", "task_id", "agent_id", "attempt"):
        assert absent not in event


def test_log_level_is_honoured() -> None:
    stream = StringIO()
    setup_json_logging(stream, log_level="WARNING")
    log = get_logger("test")
    log.info("不该出现")
    log.warning("该出现")

    (event,) = emitted(stream)
    assert event["event"] == "该出现"


def test_configure_twice_does_not_duplicate_output() -> None:
    """重复配置不能让同一条日志打印两遍。

    uvicorn --reload 会重新导入应用，`create_app()` 就跟着再跑一次。
    用 addHandler 的写法在这种情况下会越叠越多。
    """
    stream = StringIO()
    setup_json_logging(stream)
    setup_json_logging(stream)
    get_logger("test").info("只此一条")

    assert len(emitted(stream)) == 1


# ══════════════════════════════════════════════════════════
# 二、脱敏
# ══════════════════════════════════════════════════════════


def scrub(**fields: Any) -> dict[str, Any]:
    """直接过一遍脱敏处理器，省去搭日志管道。"""
    return dict(redact_secrets(None, "info", dict(fields)))


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "openai_api_key",
        "ANTHROPIC_API_KEY",
        "github_token",
        "admin_token",
        "password",
        "authorization",
        "aws_secret_key",
        "credentials",
    ],
)
def test_sensitive_field_names_are_masked(field: str) -> None:
    assert scrub(**{field: "whatever-the-value-is"})[field] == MASK


@pytest.mark.parametrize("field", ["prompt_tokens", "total_tokens", "completion_tokens"])
def test_token_counts_are_not_masked(field: str) -> None:
    """token 用量是正经数据，不能被"字段名里有 token"误伤。

    这个项目要按题统计费用（DEL-05 要 token 分布），把 `total_tokens` 打成 ***
    的话费用报表就没数据了。所以匹配规则是"以敏感词**结尾**"而不是"包含"。
    """
    assert scrub(**{field: 1234})[field] == 1234


def test_registered_secret_is_masked_anywhere_in_text() -> None:
    """被测 AI 在 stdout 里回显了自己的 Key —— 这是最现实的一条泄漏路径。

    CLI 工具启动时打一行自己的配置就够了，而我们会把 Agent 的 stdout 记进日志。
    字段名叫 `stdout_line`，一点都不敏感，只能靠比对配置里的明文来挡。
    """
    register_secret(FAKE_ANTHROPIC)
    result = scrub(stdout_line=f"aider 已启动，使用 {FAKE_ANTHROPIC} 连接 Anthropic")
    assert FAKE_ANTHROPIC not in result["stdout_line"]
    assert MASK in result["stdout_line"]
    assert "aider 已启动" in result["stdout_line"], "只该打掉密钥，别的内容要留着"


@pytest.mark.parametrize(
    "secret",
    [
        FAKE_ANTHROPIC,
        _fake_secret("sk-", "FAKEfake0123456789abcdefghij"),
        FAKE_GITHUB,
        _fake_secret("github_pat_", "FAKEfake0123456789abcdefghijkl"),
        _fake_secret("AKIA", "FAKEFAKEFAKEFAKE"),
        _fake_secret("xoxb-", "FAKEfake-0123456789"),
        _fake_secret("LTAI", "FAKEfake012345"),
    ],
)
def test_known_secret_shapes_are_masked(secret: str) -> None:
    """没在配置里出现过的第三方密钥，靠格式兜底。

    比如被测 AI 自己在代码里写死了一个 Key，或者 issue 正文里贴了别人的 token。
    """
    assert secret not in scrub(message=f"前面 {secret} 后面")["message"]


def test_masking_reaches_nested_structures() -> None:
    """字段值是 dict 或 list 时也要洗到 —— 日志里经常塞一小段配置或请求头。"""
    result = scrub(
        request={"headers": {"authorization": "Bearer abc"}, "url": "https://api.test"},
        lines=[f"key={FAKE_GITHUB}", "正常一行"],
    )
    assert result["request"]["headers"]["authorization"] == MASK
    assert result["request"]["url"] == "https://api.test"
    assert FAKE_GITHUB not in result["lines"][0]
    assert result["lines"][1] == "正常一行"


def test_short_values_are_not_registered() -> None:
    """太短的值不登记，否则日志里所有含这段字符的地方都会被打掉，日志就没法看了。"""
    register_secret("dev")
    assert scrub(message="developer 模式")["message"] == "developer 模式"


def test_configure_logging_registers_settings_secrets() -> None:
    """`configure_logging()` 要自动把配置里的密钥登记进脱敏器。

    这一步不能靠调用方记得手动登记 —— 靠记，迟早会漏。
    """
    stream = StringIO()
    settings = Settings(
        _env_file=None,
        log_format="json",
        ANTHROPIC_API_KEY=FAKE_ANTHROPIC,
        GITHUB_TOKEN=FAKE_GITHUB,
    )
    configure_logging(settings, stream=stream)
    get_logger("test").info("Agent 输出", stdout_line=f"{FAKE_ANTHROPIC} 和 {FAKE_GITHUB}")

    rendered = stream.getvalue()
    assert FAKE_ANTHROPIC not in rendered
    assert FAKE_GITHUB not in rendered


def test_third_party_stdlib_logs_are_scrubbed() -> None:
    """不走 structlog 的库（uvicorn、SQLAlchemy）打的日志也要洗。

    SQLAlchemy 打连接串时会带上密码，uvicorn 记访问日志时会带上 URL 上的 token。
    这些库直接用 stdlib logging，structlog 的处理器碰不到它们，
    所以脱敏还有一层落在 handler 的 formatter 上。
    """
    stream = StringIO()
    setup_json_logging(stream)
    register_secret(FAKE_GITHUB)

    logging.getLogger("sqlalchemy.engine").warning("连接失败 token=%s", FAKE_GITHUB)

    rendered = stream.getvalue()
    assert FAKE_GITHUB not in rendered
    assert MASK in rendered


def test_exception_is_one_parseable_json_line() -> None:
    """异常也只产生一条 JSON 记录，堆栈在 `exception` 字段里。

    这里有个具体的坑：`.exception()` 默认会走 `logging.Logger.exception()`，
    而它内部强制再设一次 `exc_info=True`，于是 handler 又在 JSON 后面追加一份
    没脱敏的堆栈原文 —— 内容一样，但只有 JSON 里那份洗过。
    """
    stream = StringIO()
    setup_json_logging(stream)

    try:
        raise RuntimeError("炸了")
    except RuntimeError:
        get_logger("test").exception("调用 Agent 失败")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"堆栈被打了不止一遍：{lines}"

    (event,) = emitted(stream)
    assert event["event"] == "调用 Agent 失败"
    assert "RuntimeError: 炸了" in event["exception"]
    assert stream.getvalue().count("RuntimeError: 炸了") == 1


def test_exception_traceback_is_scrubbed() -> None:
    """异常信息里的密钥也要洗掉。

    401 的响应体常常把请求头原样回显，异常消息一路冒泡到日志里就带出去了。
    所以脱敏处理器排在 `format_exc_info` **后面**：堆栈先变成文本，才洗得到。
    """
    stream = StringIO()
    setup_json_logging(stream)
    register_secret(FAKE_ANTHROPIC)

    try:
        raise RuntimeError(f"认证失败：x-api-key={FAKE_ANTHROPIC}")
    except RuntimeError:
        get_logger("test").exception("调用 Agent 失败")

    rendered = stream.getvalue()
    assert FAKE_ANTHROPIC not in rendered
    assert MASK in rendered
