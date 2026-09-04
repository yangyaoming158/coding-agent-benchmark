"""Runner 协议的报文模型与解析（E3-T1）。

**最要紧的是防泄题那一组**（`test_*_leak*` / `test_forbidden_*`）：任务输入里
多一个 `fail_to_pass` 或者 `test_patch_paths`，等于把答案和答案的位置一起发给了
被测 AI（协议 C-76）。这类错误不会报错、不会变慢，只会让解决率虚高，
而且事后从结果里根本看不出来。

第二组是解析（`test_parse_*`）。真实 CLI 的 stdout 是一坨刷屏日志，
"只认最后一行 JSON"这条规则要顶得住进度条、尾部空行和长得像 JSON 的日志行。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.enums import CostSource, IssueLanguage
from app.runner.protocol import (
    FORBIDDEN_INPUT_KEYS,
    RUNNER_PROTOCOL_VERSION,
    AgentConfig,
    AgentRunner,
    AgentRunResult,
    AgentTaskInput,
    Constraints,
    IssueInput,
    LeakyInputError,
    ModelInput,
    ProbeResult,
    RepoInput,
    ResultParseError,
    TokenUsage,
    assert_no_leak,
    parse_result_line,
    parse_result_stdout,
    read_result,
)

SAMPLE_COMMIT = "a" * 40


def make_input(**overrides: object) -> AgentTaskInput:
    """造一份合法的任务输入，只写这条用例关心的字段。"""
    payload: dict[str, object] = {
        "task_id": "example__demo-1",
        "issue": IssueInput(title="标题", body="正文", language=IssueLanguage.ZH),
        "repo": RepoInput(name="example/demo", base_commit=SAMPLE_COMMIT),
        "constraints": Constraints(deadline_unix_ms=int(time.time() * 1000) + 60_000),
        "model": ModelInput(name="fake-model"),
    }
    payload.update(overrides)
    return AgentTaskInput(**payload)  # type: ignore[arg-type]


def make_result(**overrides: object) -> AgentRunResult:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "agent_name": "reference",
        "started_at": now,
        "finished_at": now + timedelta(seconds=1),
        "duration_ms": 1000,
    }
    payload.update(overrides)
    return AgentRunResult(**payload)  # type: ignore[arg-type]


# ── 防泄题：任务输入里不能夹带答案（协议 C-76）─────────────


@pytest.mark.parametrize("key", sorted(FORBIDDEN_INPUT_KEYS))
def test_forbidden_key_at_top_level_is_rejected(key: str) -> None:
    """名单里的每个键都不能作为顶层字段出现。

    逐个跑而不是随便挑一个：名单是手写的，漏掉哪个不会有人发现，
    而漏掉的那个正好就是下次泄题的入口。
    """
    payload = json.loads(make_input().model_dump_json())
    payload[key] = "答案"
    with pytest.raises(ValidationError):
        AgentTaskInput.model_validate(payload)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_INPUT_KEYS))
def test_forbidden_key_inside_extra_is_rejected(key: str) -> None:
    """`extra` 是适配器私有配置的口子，也是泄题最容易发生的地方。

    它不受字段声明约束，往里面塞一个 `fail_to_pass` 谁都不会发现——
    除非像这里一样专门扫一遍。
    """
    with pytest.raises(ValidationError, match="C-76"):
        make_input(extra={key: ["tests/test_a.py::test_x"]})


def test_forbidden_key_nested_deep_in_extra_is_rejected() -> None:
    """嵌套着藏也要能查出来。只查顶层的话，包一层字典就绕过去了。"""
    with pytest.raises(ValidationError, match="C-76"):
        make_input(extra={"adapter": {"debug": {"test_patch_paths": ["tests/x.py"]}}})


def test_forbidden_key_inside_a_list_is_rejected() -> None:
    with pytest.raises(ValidationError, match="C-76"):
        make_input(extra={"presets": [{"name": "a"}, {"gold_patch": "diff --git ..."}]})


def test_assert_no_leak_guards_hand_built_payloads() -> None:
    """给"绕过模型直接拼字典"的路径用的闸：适配器手工组报文、或者从文件重放。"""
    assert_no_leak({"task_id": "x", "issue": {"title": "t"}})
    with pytest.raises(LeakyInputError, match="fail_to_pass"):
        assert_no_leak({"task_id": "x", "fail_to_pass": ["tests/a.py::test_b"]})


def test_no_task_input_field_is_on_the_forbidden_list() -> None:
    """模型自己的字段名不能和禁发名单撞车。

    撞车意味着有人把一个本该瞒着 AI 的东西定义成了正式字段——
    那样上面两组用例全都拦不住它，因为它成了"合法字段"。
    """
    assert not set(AgentTaskInput.model_fields) & FORBIDDEN_INPUT_KEYS


def test_repo_input_carries_no_url() -> None:
    """只给仓库名和 base commit，不给 URL。

    给了 URL，AI 一句 `git clone` 就能拉到官方修复——工作区里剥离历史的功夫
    （E2-T1）就白做了。
    """
    assert "url" not in " ".join(RepoInput.model_fields)


# ── 结果模型：判定权不在适配器手里（协议纪律 2）─────────────


def test_result_has_no_resolved_field() -> None:
    """`AgentRunResult` 里没有 `resolved`，也不许加。

    判定必须 100% 由测试结果推导。让适配器报"我修好了"，等于把判定权交给被测方。
    """
    assert "resolved" not in AgentRunResult.model_fields


def test_result_rejects_unknown_fields() -> None:
    """多写一个字段直接报错，不是忽略掉。

    忽略掉的后果是：一个报了 `resolved` 的适配器看起来一切正常，
    而它想表达的意思被静默丢弃了——两边都以为对方处理了这件事。
    """
    payload = json.loads(make_result().model_dump_json())
    payload["resolved"] = True
    with pytest.raises(ValidationError):
        AgentRunResult.model_validate(payload)


@pytest.mark.parametrize(
    ("cost_source", "cost_usd"),
    [
        (CostSource.UNAVAILABLE, 0.0),  # 报不出来却填了 0：成本统计会悄悄偏低
        (CostSource.REPORTED, None),  # 说报得出却没数字：报表上一个空洞
        (CostSource.ESTIMATED, None),
    ],
)
def test_cost_source_and_amount_must_agree(cost_source: CostSource, cost_usd: float | None) -> None:
    with pytest.raises(ValidationError):
        make_result(cost_source=cost_source, cost_usd=cost_usd)


def test_unavailable_cost_is_the_default() -> None:
    """默认就是"拿不到"。订阅制 CLI 报不出金额是常态，默认值应该是最诚实的那个。"""
    assert make_result().cost_source is CostSource.UNAVAILABLE


def test_token_total_is_filled_when_missing() -> None:
    assert TokenUsage(input=100, output=20).total == 120


def test_token_total_reported_by_the_adapter_is_kept() -> None:
    """适配器报了 total 就用它的。

    各家 CLI 报的 total 含不含缓存读并不统一，我们没有资格替它校正——
    校正过头会让"平台报的数"和"CLI 自己报的数"对不上，排查时两边都不可信。
    """
    assert TokenUsage(input=100, output=20, cache_read=900, total=1020).total == 1020


def test_finished_before_started_is_rejected() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        make_result(started_at=now, finished_at=now - timedelta(seconds=1))


def test_has_patch_ignores_whitespace_only_patches() -> None:
    """只有空白的补丁算空补丁。

    有的 CLI 在没改东西时会输出一个换行，当成"有补丁"的话，
    空补丁率这个指标就废了。
    """
    assert not make_result(patch="   \n\t\n").has_patch
    assert make_result(patch="diff --git a/x b/x\n").has_patch


# ── 解析：只认最后一行 JSON（协议纪律 1）───────────────────


def result_line(**overrides: object) -> str:
    return json.dumps(json.loads(make_result(**overrides).model_dump_json()), ensure_ascii=False)


def test_parse_picks_the_last_line_not_the_first() -> None:
    """前面那行长得像结果也不算。真实 CLI 会把收到的任务原样回显出来。"""
    noise = '{"protocol_version": "1.0", "agent_name": "回显的日志"}'
    line = result_line(agent_name="真正的结果")
    assert parse_result_stdout(f"{noise}\n{line}").agent_name == "真正的结果"


def test_parse_survives_trailing_blank_lines() -> None:
    """几乎所有 CLI 都会在最后多打一个换行。"""
    assert parse_result_stdout(result_line() + "\n\n\n").agent_name == "reference"


def test_parse_survives_a_progress_bar() -> None:
    """进度条用 `\\r` 在同一行反复重写，最后一行读出来是 `进度\\r{...}`。"""
    stdout = f"下载中 40%\r下载中 90%\r{result_line()}"
    assert parse_result_stdout(stdout).agent_name == "reference"


def test_parse_survives_a_bom() -> None:
    """有的工具会在输出开头写 BOM，`json.loads` 认不了。"""
    assert parse_result_line("﻿" + result_line()).agent_name == "reference"


def test_parse_survives_hundreds_of_log_lines() -> None:
    noise = "\n".join(f"[info] 第 {i} 步" for i in range(500))
    assert parse_result_stdout(f"{noise}\n{result_line()}").agent_name == "reference"


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "没有任何输出"),
        ("\n  \n", "没有任何输出"),
        ("[info] 干完了", "不是合法 JSON"),
        ("[1, 2, 3]", "必须是一个 JSON 对象"),
        ('{"agent_name": "x"}', "不合协议"),  # 缺时间字段
    ],
)
def test_parse_failures_say_what_went_wrong(stdout: str, reason: str) -> None:
    """读不出来时报错要能定位。

    只说"解析失败"的话，排查得从几千行日志里自己找最后一行，
    而那正是这条报错本该告诉你的东西。
    """
    with pytest.raises(ResultParseError, match=reason):
        parse_result_stdout(stdout)


def test_read_result_prefers_the_file(tmp_path: Path) -> None:
    """结果文件优先。stdout 是和 CLI 日志共用的通道，容易被别人搅掉。"""
    path = tmp_path / "result.json"
    path.write_text(result_line(agent_name="来自文件"), encoding="utf-8")
    got = read_result(result_line(agent_name="来自stdout"), result_file=path)
    assert got.agent_name == "来自文件"


def test_read_result_falls_back_when_the_file_is_absent(tmp_path: Path) -> None:
    got = read_result(result_line(agent_name="来自stdout"), result_file=tmp_path / "nope.json")
    assert got.agent_name == "来自stdout"


def test_broken_result_file_does_not_fall_back(tmp_path: Path) -> None:
    """文件在但内容坏了要报错，**不能**悄悄回到 stdout。

    静默回退会把"适配器写了个坏文件"伪装成"适配器没写文件"，
    下次同样的问题还是查不出来。
    """
    path = tmp_path / "result.json"
    path.write_text("{坏了", encoding="utf-8")
    with pytest.raises(ResultParseError):
        read_result(result_line(), result_file=path)


# ── 序列化与其他 ────────────────────────────────────────────


def test_stdin_line_round_trips() -> None:
    task = make_input()
    assert AgentTaskInput.model_validate(json.loads(task.to_stdin_line())) == task


def test_stdin_line_keeps_chinese_readable() -> None:
    """不转义成 `\\uXXXX`：适配器日志里会原样回显这行，转义之后没法一眼看出任务是什么。"""
    task = make_input(issue=IssueInput(title="空密码放行", body="正文", language=IssueLanguage.ZH))
    assert "空密码放行" in task.to_stdin_line()


def test_remaining_ms_clamps_at_zero() -> None:
    past = Constraints(deadline_unix_ms=1_000)
    assert past.remaining_ms(now_ms=2_000) == 0
    assert past.remaining_ms(now_ms=400) == 600


def test_protocol_version_is_pinned() -> None:
    """版本号写死成 Literal，报文里带别的值会被拒。

    协议改了要同时改这里和 `04-runner-protocol.md`；不写死的话，
    一个老适配器发上来的旧报文会被当成新协议解析，字段缺失的表现是"某些统计是空的"。
    """
    assert make_input().protocol_version == RUNNER_PROTOCOL_VERSION
    payload = json.loads(make_input().model_dump_json())
    payload["protocol_version"] = "0.9"
    with pytest.raises(ValidationError):
        AgentTaskInput.model_validate(payload)


def test_agent_runner_protocol_is_structural() -> None:
    """`AgentRunner` 是结构化协议：长得对就算实现了，不用继承。

    这让写适配器的人不必依赖我们的基类，也让测试里造假适配器成本极低。
    """

    class Fine:
        name = "fine"

        def probe(self) -> ProbeResult:
            return ProbeResult(ok=True)

        def run(self, task: object, workspace: object, config: object) -> None:
            return None

    class MissingRun:
        name = "broken"

        def probe(self) -> ProbeResult:
            return ProbeResult(ok=True)

    assert isinstance(Fine(), AgentRunner)
    assert not isinstance(MissingRun(), AgentRunner)


def test_agent_config_defaults_are_empty() -> None:
    """harness 侧配置默认什么都不给，逼调用方显式说明要注入什么。"""
    config = AgentConfig()
    assert config.image is None
    assert dict(config.env) == {}
    assert config.result_file is None
