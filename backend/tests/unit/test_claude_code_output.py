"""从 Claude Code 的 stream-json 里读东西（E3-T5）。

这一组**不起容器、不调模型、不碰工作区**：喂一段事先写好的 JSONL，看解析对不对。
它盯的是最容易安静出错的两处 ——

- **token 口径**：Anthropic 的 `input_tokens` 不含缓存，平台的口径是
  `cache_read ⊆ input`。折算错了不会报错，只会让输入量少报九成。
- **cost 的可信度**：`total_cost_usd` 是 CLI 自己按价目表算的。走中转端点时
  那个数可能是 0，而 0 和"读不出来"在报表里是两回事（协议纪律 3）。

真把 claude 跑起来的是 `tests/contract/test_claude_code_runner.py`（要 Key、花钱）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.runner.adapters.claude_code import (
    CREDENTIAL_VARS,
    ClaudeCodeRunner,
    args_digest,
    build_command,
    build_trajectory,
    credential_env,
    init_event,
    parse_events,
    parse_usage,
    parse_version,
    per_call_usage,
    result_event,
    tool_summary,
)
from app.runner.protocol import AgentTaskInput
from app.sandbox.container import AGENT_ENV_ALLOWLIST, DETERMINISM_ENV
from tests.contract.runner_contract import make_task_input

STARTED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

#: 合成任务的截止时刻。解析层用不到它，随手给一个足够远的值就行。
FAR_FUTURE_MS = 4_000_000_000_000


def a_task() -> AgentTaskInput:
    return make_task_input(deadline_ms=FAR_FUTURE_MS)


#: 假 Key。**拼出来而不是整串写死** —— 整串写会被提交前的密钥扫描器拦下。
FAKE_KEY = "sk-" + "test" + "0" * 20


def usage(
    *, input_tokens: int = 0, output: int = 0, cache_read: int = 0, cache_creation: int = 0
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def assistant(
    *blocks: dict[str, Any],
    usage_block: dict[str, int] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """一条 assistant 事件。

    `message_id` 是真实报文里就有的字段，而且**一次 API 调用会发出多条带同一个 id
    的事件** —— 用量去重全靠它，所以合成样本里也带上。
    """
    message: dict[str, Any] = {"role": "assistant", "content": list(blocks)}
    if message_id is not None:
        message["id"] = message_id
    if usage_block is not None:
        message["usage"] = usage_block
    return {"type": "assistant", "message": message, "session_id": "s1"}


def tool_use(name: str, args: dict[str, Any], *, tool_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def tool_result(*, tool_id: str = "toolu_1", is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": "ok",
                    "is_error": is_error,
                }
            ],
        },
    }


def init(**extra: Any) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "cwd": "/workspace",
        "session_id": "s1",
        "model": "deepseek-chat",
        "permissionMode": "bypassPermissions",
        **extra,
    }


def result(
    *,
    subtype: str = "success",
    is_error: bool = False,
    num_turns: int = 6,
    cost: float | None = 0.0421,
    usage_block: dict[str, int] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": num_turns,
        "duration_ms": 12345,
        "session_id": "s1",
    }
    if cost is not None:
        event["total_cost_usd"] = cost
    if usage_block is not None:
        event["usage"] = usage_block
    return event


def stream(*events: dict[str, Any]) -> str:
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


# ── 切事件 ──────────────────────────────────────────────────


def test_plain_log_lines_are_skipped() -> None:
    """协议纪律 1：前面允许任意日志，真实 CLI 一定会刷屏。"""
    stdout = "npm warn something\n" + stream(init(), result()) + "\nbye\n"
    assert [e["type"] for e in parse_events(stdout)] == ["system", "result"]


def test_a_half_written_last_line_is_dropped_not_fatal() -> None:
    """容器被超时杀掉时最后一行常常只写了一半。

    这一行必须能安静丢掉：协议 C-09a 要求超时也保存补丁，
    要是解析在这里抛异常，一次正常的超时就变成了一次崩溃。
    """
    stdout = stream(assistant({"type": "text", "text": "hi"}, usage_block=usage(input_tokens=10)))
    stdout += '{"type":"assist'
    events = parse_events(stdout)
    assert len(events) == 1


def test_json_that_is_not_an_object_is_skipped() -> None:
    assert parse_events('[1, 2]\n"hello"\n') == []


def test_result_event_is_the_last_one() -> None:
    events = parse_events(stream(result(num_turns=1), result(num_turns=9)))
    found = result_event(events)
    assert found is not None and found["num_turns"] == 9


def test_no_result_event_reads_as_none() -> None:
    assert result_event(parse_events(stream(init()))) is None


def test_init_event_is_found() -> None:
    found = init_event(parse_events(stream(init(), result())))
    assert found is not None and found["model"] == "deepseek-chat"


# ── 用量 ────────────────────────────────────────────────────


def test_tokens_are_summed_across_messages() -> None:
    events = parse_events(
        stream(
            assistant({"type": "text", "text": "a"}, usage_block=usage(input_tokens=10, output=2)),
            assistant({"type": "text", "text": "b"}, usage_block=usage(input_tokens=30, output=5)),
            result(),
        )
    )
    got = parse_usage(events, trust_cost=True)
    assert got is not None
    assert (got.input_tokens, got.output_tokens) == (40, 7)


def test_cached_tokens_count_as_input() -> None:
    """最容易安静出错的一条。

    Anthropic 的 `input_tokens` **不含**缓存那部分，缓存另外用两个字段报；
    而平台的口径是 `cache_read` 是 `input` 的一部分（迁移 0003 的说明）。
    照搬 `input_tokens` 的话，一次命中缓存的运行会少报九成输入量，
    按 token 估的成本跟着一起偏低，而且没有任何报错。
    """
    events = parse_events(
        stream(
            assistant(
                {"type": "text", "text": "a"},
                usage_block=usage(input_tokens=100, output=20, cache_read=9000, cache_creation=500),
            )
        )
    )
    got = parse_usage(events, trust_cost=True)
    assert got is not None
    assert got.input_tokens == 100 + 500 + 9000
    assert got.cache_read_tokens == 9000
    assert got.output_tokens == 20


def test_cost_comes_from_the_result_event() -> None:
    events = parse_events(stream(assistant(usage_block=usage(input_tokens=1)), result(cost=0.5)))
    got = parse_usage(events, trust_cost=True)
    assert got is not None and got.cost_usd == 0.5


def test_cost_is_not_trusted_behind_a_gateway() -> None:
    """走中转端点时 `total_cost_usd` 是 CLI 按自己那张价目表算的，认不出模型就是 0。

    宁可报 unavailable 让平台去估，也不能把这个 0 当成"真没花钱"（协议纪律 3）。
    """
    events = parse_events(stream(assistant(usage_block=usage(input_tokens=1)), result(cost=0.0)))
    got = parse_usage(events, trust_cost=False)
    assert got is not None and got.cost_usd is None
    assert got.input_tokens == 1


def test_tokens_survive_a_run_with_no_result_event() -> None:
    """被杀在半路也要留下用量证据 —— 光看 result 事件的话这次会是一片空白。"""
    events = parse_events(
        stream(
            assistant(usage_block=usage(input_tokens=10, output=1)),
            assistant(usage_block=usage(input_tokens=20, output=3)),
        )
    )
    got = parse_usage(events, trust_cost=True)
    assert got is not None
    assert (got.input_tokens, got.output_tokens, got.cost_usd) == (30, 4, None)
    # 没有 result 事件时退回"数了几次 API 调用"
    assert got.turns == 2


def test_turns_come_from_the_cli_not_from_our_own_count() -> None:
    """`turns` 要和 `--max-turns` 同一个刻度，否则没法回答"预算是不是用满了"。"""
    events = parse_events(
        stream(assistant(usage_block=usage(input_tokens=1)), result(num_turns=17))
    )
    got = parse_usage(events, trust_cost=True)
    assert got is not None and got.turns == 17


def test_the_result_event_usage_wins() -> None:
    """实测下来它才是权威：逐条消息的 `output_tokens` 全是 0。"""
    events = parse_events(stream(result(usage_block=usage(input_tokens=7, output=2))))
    got = parse_usage(events, trust_cost=True)
    assert got is not None and (got.input_tokens, got.output_tokens) == (7, 2)


def test_nothing_readable_is_none_not_zero() -> None:
    """读不出来就说读不出来。填 0 会让成本统计悄悄偏低（协议纪律 3）。"""
    assert parse_usage(parse_events(stream(init())), trust_cost=True) is None


def test_one_api_call_that_emits_two_events_is_only_counted_once() -> None:
    """2026-09-06 实测踩到的坑：一次调用发两条 assistant 事件（思考一条、

    工具调用一条），**两条带着同一份 usage**。照事件求和输入 token 正好翻倍
    （实测 40038 vs 真实 20019），而且不报任何错。
    """
    events = parse_events(
        stream(
            assistant(
                {"type": "thinking", "thinking": "想想"},
                usage_block=usage(input_tokens=19705),
                message_id="m1",
            ),
            assistant(
                tool_use("Read", {"file_path": "a.py"}),
                usage_block=usage(input_tokens=19705),
                message_id="m1",
            ),
        )
    )
    assert len(per_call_usage(events)) == 1
    got = parse_usage(events, trust_cost=True)
    assert got is not None and got.input_tokens == 19705


def test_a_message_with_no_id_is_still_counted() -> None:
    """宁可多算也不漏 —— 漏了看不出来，多了至少数字异常。"""
    events = parse_events(
        stream(
            assistant(usage_block=usage(input_tokens=5)),
            assistant(usage_block=usage(input_tokens=5)),
        )
    )
    got = parse_usage(events, trust_cost=True)
    assert got is not None and got.input_tokens == 10


def test_version_comes_from_the_real_field_name() -> None:
    """实测字段叫 `claude_code_version`。按 `version` 读的话永远读不到，

    于是每次都退回镜像里钉的常量 —— 报表上就会写着一个没跑过的版本号。
    """
    assert parse_version(parse_events(stream(init(claude_code_version="2.1.236")))) == "2.1.236"


def test_version_comes_from_the_init_event() -> None:
    assert parse_version(parse_events(stream(init(version="2.1.236")))) == "2.1.236"


def test_missing_version_reads_as_none() -> None:
    assert parse_version(parse_events(stream(init()))) is None


# ── 轨迹 ────────────────────────────────────────────────────


def trajectory_of(*events: dict[str, Any]) -> list[dict[str, Any]]:
    text = build_trajectory(parse_events(stream(*events)), started_at=STARTED_AT)
    return [json.loads(line) for line in text.splitlines()]


def test_the_tool_call_sequence_can_be_reconstructed() -> None:
    """E3-T5 比 E3-T4 多出来的那条验收标准，就是这一条。

    Aider 只能从 `Applied edit to <path>` 认出"改了文件"；这里连
    读了哪个文件、搜了什么、跑了什么命令，顺序都在。
    """
    rows = trajectory_of(
        assistant(tool_use("Read", {"file_path": "/workspace/auth/password.py"}, tool_id="t1")),
        tool_result(tool_id="t1"),
        assistant(tool_use("Grep", {"pattern": "def verify"}, tool_id="t2")),
        tool_result(tool_id="t2"),
        assistant(tool_use("Edit", {"file_path": "/workspace/auth/password.py"}, tool_id="t3")),
        tool_result(tool_id="t3"),
    )
    calls = [(r["name"], r["summary"]) for r in rows if r["type"] == "tool_call"]
    assert calls == [
        ("Read", "auth/password.py"),
        ("Grep", "def verify"),
        ("Edit", "auth/password.py"),
    ]


def test_a_failed_tool_call_is_marked() -> None:
    """哪一步失败了要看得见 —— 失败归因（E6）拿它当证据。"""
    rows = trajectory_of(
        assistant(tool_use("Bash", {"command": "pytest"}, tool_id="t1")),
        tool_result(tool_id="t1", is_error=True),
    )
    call = next(r for r in rows if r["type"] == "tool_call")
    assert call["ok"] is False


def test_a_tool_call_with_no_result_has_no_ok_field() -> None:
    """结果还没回来（被杀在半路）时不编一个 ok 出来。"""
    rows = trajectory_of(assistant(tool_use("Read", {"file_path": "a.py"}, tool_id="t1")))
    assert "ok" not in next(r for r in rows if r["type"] == "tool_call")


def test_assistant_text_becomes_a_message_event() -> None:
    """这次敢写 `message` 事件，是因为角色是 CLI 自己标的，不是我们猜的。"""
    rows = trajectory_of(assistant({"type": "text", "text": "先看看这个函数"}))
    assert rows[0]["type"] == "message"
    assert rows[0]["role"] == "assistant"
    assert rows[0]["text_excerpt"] == "先看看这个函数"


def test_empty_text_blocks_are_not_recorded() -> None:
    assert trajectory_of(assistant({"type": "text", "text": "   "})) == []


def test_usage_lands_as_its_own_event() -> None:
    rows = trajectory_of(
        assistant({"type": "text", "text": "hi"}, usage_block=usage(input_tokens=8, output=3))
    )
    llm = next(r for r in rows if r["type"] == "llm_usage")
    assert (llm["input"], llm["output"]) == (8, 3)


def test_seq_increases_and_ts_is_the_start_time() -> None:
    """事件流本身不带时间戳，所以老实标序号，不去插值编时间。"""
    rows = trajectory_of(
        assistant({"type": "text", "text": "a"}, usage_block=usage(input_tokens=1)),
        assistant(tool_use("Read", {"file_path": "a.py"}), usage_block=usage(input_tokens=2)),
    )
    assert [r["seq"] for r in rows] == list(range(len(rows)))
    assert {r["ts"] for r in rows} == {int(STARTED_AT.timestamp() * 1000)}


def test_tool_args_are_a_digest_not_the_original() -> None:
    """`Edit` 的参数里是整段旧文本和新文本，原样写进轨迹等于把补丁又存了一遍。"""
    rows = trajectory_of(
        assistant(tool_use("Edit", {"file_path": "a.py", "old_string": "x" * 5000}))
    )
    call = rows[0]
    assert call["args_digest"].startswith("sha256:")
    assert "x" * 100 not in json.dumps(call)


def test_the_digest_tells_different_arguments_apart() -> None:
    assert args_digest({"a": 1}) != args_digest({"a": 2})
    assert args_digest({"a": 1, "b": 2}) == args_digest({"b": 2, "a": 1})


def test_summary_falls_back_through_the_key_list() -> None:
    assert tool_summary({"command": "pytest -q"}) == "pytest -q"
    assert tool_summary({"pattern": "def foo"}) == "def foo"
    assert tool_summary({"nothing": "useful"}) == ""


def test_summary_drops_the_container_path_prefix() -> None:
    assert tool_summary({"file_path": "/workspace/pkg/mod.py"}) == "pkg/mod.py"


# ── 命令 ────────────────────────────────────────────────────


def test_the_four_mandatory_flags_are_there() -> None:
    """少任何一个的后果见模块开头那张表：要么拿不到 token，要么挂到超时。"""
    command = build_command(a_task(), "deepseek-chat", max_turns=40)
    assert "--print" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"


def test_max_turns_is_passed_through() -> None:
    command = build_command(a_task(), "m", max_turns=7)
    assert command[command.index("--max-turns") + 1] == "7"


def test_the_prompt_is_the_last_argument() -> None:
    """夹在中间的话，题干里以 `-` 开头的一行可能被当成新的开关。"""
    task = a_task()
    command = build_command(task, "m", max_turns=3)
    assert command[-1].startswith(("Please fix", "请修复"))
    assert task.issue.title in command[-1]


def test_both_agents_send_the_same_prompt() -> None:
    """排行榜比的是 Agent，不是提示词。两边各写各的话，比出来的是后者。"""
    from app.runner.adapters.aider import build_command as aider_command

    task = a_task()
    claude = build_command(task, "m", max_turns=3)[-1]
    aider = aider_command(task, "m")[-1]
    assert claude == aider


# ── 凭据 ────────────────────────────────────────────────────


def test_the_official_endpoint_passes_the_key_through() -> None:
    env = {**DETERMINISM_ENV, "ANTHROPIC_API_KEY": FAKE_KEY}
    got = credential_env(env, base_url=None)
    assert got["ANTHROPIC_API_KEY"] == FAKE_KEY
    assert "ANTHROPIC_BASE_URL" not in got


def test_a_gateway_renames_the_key_to_what_the_cli_reads() -> None:
    """`agent_env_for()` 挑出来的是 DeepSeek 的 Key，而 claude 只认 ANTHROPIC_*。"""
    got = credential_env(
        {**DETERMINISM_ENV, "DEEPSEEK_API_KEY": FAKE_KEY},
        base_url="https://api.deepseek.com/anthropic",
    )
    assert got["ANTHROPIC_AUTH_TOKEN"] == FAKE_KEY
    assert got["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"


def test_the_original_key_does_not_stay_in_the_container() -> None:
    """同一个字符串，但被测对象是会执行任意代码的 AI，多一个变量就多一份暴露。"""
    got = credential_env({"DEEPSEEK_API_KEY": FAKE_KEY}, base_url="https://gw.example/anthropic")
    assert "DEEPSEEK_API_KEY" not in got


def test_the_determinism_variables_survive() -> None:
    got = credential_env({**DETERMINISM_ENV, "DEEPSEEK_API_KEY": FAKE_KEY}, base_url="https://gw")
    assert got["TZ"] == "UTC" and got["PYTHONHASHSEED"] == "0"


def test_no_key_at_all_is_not_a_crash() -> None:
    """让 claude 自己去报"没有凭据"，那条报错会被认成鉴权失败，日志里看得见。"""
    got = credential_env(dict(DETERMINISM_ENV), base_url="https://gw")
    assert "ANTHROPIC_AUTH_TOKEN" not in got
    assert got["ANTHROPIC_BASE_URL"] == "https://gw"


def test_every_variable_we_inject_is_on_the_allowlist() -> None:
    """加一个新名字忘了加白名单的话，表现是容器里少一个变量，

    而 CLI 只会说"没有 API Key" —— 排查时根本想不到是白名单挡的。
    """
    assert set(CREDENTIAL_VARS) <= AGENT_ENV_ALLOWLIST


# ── 从配置造适配器 ──────────────────────────────────────────


def test_params_decide_the_endpoint_and_the_budget() -> None:
    runner = ClaudeCodeRunner.from_params({"base_url": "https://gw", "max_turns": 12})
    command = build_command(a_task(), "m", max_turns=runner._max_turns)
    assert command[command.index("--max-turns") + 1] == "12"
    assert runner._base_url == "https://gw"


def test_a_junk_budget_falls_back_instead_of_crashing() -> None:
    """params 是数据库里的 JSON，什么都可能写进去。一条配错的记录不该让作业崩掉。"""
    runner = ClaudeCodeRunner.from_params({"max_turns": "四十"})
    assert runner._max_turns == 40


def test_every_seeded_adapter_class_actually_imports() -> None:
    """`agents.adapter_class` 是数据库里的一个字符串，写错了要到跑作业时才炸。

    这条放在这里是因为 claude-code 那一行是新加的；顺手把四行全扫一遍，
    以后再加适配器也一起保着。真正建库的那份检查在
    `tests/integration/test_seed.py`（要数据库）。
    """
    from importlib import import_module

    from cli.seed import SEED_AGENTS

    for spec in SEED_AGENTS:
        module_path, _, class_name = spec.adapter_class.rpartition(".")
        runner_class = getattr(import_module(module_path), class_name)
        assert isinstance(runner_class.name, str)


def test_the_seeded_claude_config_points_at_the_gateway() -> None:
    """种子里那份配置要能原样造出适配器 —— 编排层走的就是这条路。"""
    from cli.seed import SEED_AGENTS

    spec = next(a for a in SEED_AGENTS if a.name == "claude-code")
    runner = ClaudeCodeRunner.from_params(spec.params)
    assert runner.name == "claude-code"
    assert runner._base_url == spec.params["base_url"]
    assert runner._max_turns == spec.params["max_turns"]
    # 模型名要能让 `agent_env_for()` 挑出一把 Key，否则容器里一个凭据都没有
    from app.infrastructure.config import PROVIDER_KEY_ENV

    assert any(marker in spec.model_name.lower() for marker in PROVIDER_KEY_ENV)


def test_an_empty_base_url_means_the_official_endpoint() -> None:
    assert ClaudeCodeRunner.from_params({"base_url": ""})._base_url is None
    assert ClaudeCodeRunner.from_params({})._base_url is None


# ── 录下来的真实报文 ────────────────────────────────────────
#
# 上面所有样本都是我们自己编的，共同盲区是"claude 真打出来的是不是长这样"。
# 下面这一组喂的是 2026-09-06 真跑出来的那份（见 tests/fixtures/claude_code/README.md）。
# 它当场推翻了两个假设，两条对应的用例就写在这儿。


def recorded_events() -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "fixtures/claude_code/deepseek_gateway_edit.jsonl"
    return parse_events(path.read_text(encoding="utf-8"))


def test_the_recorded_run_parses_at_all() -> None:
    events = recorded_events()
    assert result_event(events) is not None
    assert init_event(events) is not None


def test_the_recorded_totals_match_what_the_cli_itself_reported() -> None:
    """把我们算出来的数和 CLI 自己在 result 里报的数对上。

    对不上就说明聚合逻辑漂了 —— 而这件事平时不会有任何报错。
    """
    events = recorded_events()
    reported = result_event(events)
    assert reported is not None
    got = parse_usage(events, trust_cost=True)
    assert got is not None
    assert got.input_tokens == (
        reported["usage"]["input_tokens"]
        + reported["usage"]["cache_read_input_tokens"]
        + reported["usage"]["cache_creation_input_tokens"]
    )
    assert got.output_tokens == reported["usage"]["output_tokens"] == 181
    assert got.cache_read_tokens == 39552
    assert got.turns == reported["num_turns"] == 3


def test_the_recorded_run_would_have_double_counted_without_dedup() -> None:
    """这就是那个坑本身：6 条 assistant 事件，其实只有 3 次 API 调用。"""
    events = recorded_events()
    assistants = [e for e in events if e.get("type") == "assistant"]
    assert len(assistants) == 6
    assert len(per_call_usage(events)) == 3

    naive = sum(e["message"]["usage"]["input_tokens"] for e in assistants)
    assert naive == 40038  # 真实值是 20019，正好翻倍


def test_the_recorded_version_is_read_from_the_stream() -> None:
    assert parse_version(recorded_events()) == "2.1.236"


def test_the_gateway_cost_is_refused_even_though_it_is_not_zero() -> None:
    """实测：CLI 报了 $0.1244，而这次一共才 2 万 input + 181 output。

    它是拿自己那张价目表算的（stderr 里同时打了 `unrecognized_model`），
    按 DeepSeek 的实际价目算不到一美分 —— **差一个数量级**。
    这比"可能是 0"更能说明为什么走中转端点时必须报 unavailable：
    那个数字不是缺失，是错的，而错的数字会被当成真的写进报告。
    """
    events = recorded_events()
    assert result_event(events)["total_cost_usd"] > 0.12
    trusted = parse_usage(events, trust_cost=True)
    assert trusted is not None and trusted.cost_usd is not None
    gateway = parse_usage(events, trust_cost=False)
    assert gateway is not None and gateway.cost_usd is None


def test_the_recorded_trajectory_reconstructs_the_tool_sequence() -> None:
    """E3-T5 那条验收标准，用真报文再验一遍。"""
    rows = [
        json.loads(line)
        for line in build_trajectory(recorded_events(), started_at=STARTED_AT).splitlines()
    ]
    assert [(r["name"], r["summary"]) for r in rows if r["type"] == "tool_call"] == [
        ("Read", "calc.py"),
        ("Edit", "calc.py"),
    ]
    assert all(r["ok"] for r in rows if r["type"] == "tool_call")


def test_the_recorded_thinking_blocks_are_kept_and_labelled() -> None:
    """`thinking` 是 CLI 自己标好的类型，不是我们猜的，所以敢落地。

    归因（E6）要回答"它为什么改这里"，这段是最直接的证据。
    """
    rows = [
        json.loads(line)
        for line in build_trajectory(recorded_events(), started_at=STARTED_AT).splitlines()
    ]
    thinking = [r for r in rows if r["type"] == "message" and r.get("thinking")]
    # 这一份里有 3 个 thinking 块，但只有第一个有内容 —— DeepSeek 只在第一次调用
    # 返回了推理，后两次是空串。空的不落地：空的证据不是证据，只会把轨迹撑长
    assert len(thinking) == 1
    assert thinking[0]["text_excerpt"].startswith("The user says")
