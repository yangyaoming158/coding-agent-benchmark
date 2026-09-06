"""解析 aider 的输出，和拼给它的那条命令（E3-T4）。

aider 是外部工具，它的 stdout 是**自由文本**，不是我们能规定格式的报文。所以这一层
全是"从一段人类可读的输出里把数字抠出来"，也正因如此，它是这个适配器里最容易
悄悄坏掉的地方：解析不出来不会报错，只会让成本统计变成一片空白。

这里不起容器、不调模型，跑得比眨眼还快，进每次提交的快速测试集。
真的把 aider 跑起来是 `tests/contract/test_aider_runner.py`（要 Key，手动触发）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.enums import IssueLanguage
from app.runner.adapters.aider import (
    build_command,
    build_message,
    build_trajectory,
    has_model_side_failure,
    looks_like_auth_failure,
    parse_token_count,
    parse_usage,
    parse_version,
)
from tests.contract.runner_contract import make_task_input

#: 2026-09-05 第一次真跑 aider 时抓下来的原文，逐字保留（含它自己的折行位置）。
#: 自己编出来的样本永远测不到"从单词中间折"这件事。
REAL_AUTH_FAILURE_STDOUT = """\
Aider v0.86.2
Model: deepseek/deepseek-chat with diff edit format, prompt cache, infinite
output
Git repo: .git with 4 files
Repo-map: using 4096 tokens, auto refresh

litellm.BadRequestError: DeepseekException - {"error":{"message":"Authentication
Fails, Your api key: ****9ca8 is
invalid","type":"authentication_error","param":null,"code":"invalid_request_erro
r"}}
"""

#: 2026-09-05 正式跑四道 Golden 题时抓下来的用量行，逐字保留（含折行位置）。
#:
#: 它和上面那段"跑成功"的样本差两样东西，而这两样都是我编的时候没想到的：
#: 中间多了 `cache hit` 字段（DeepSeek 的提示缓存命中，冷启动时没有），
#: 以及 `session.` 被折到了下一行 —— 而且劈的位置三次都不一样。
#: 结果是四道题的 token 和 cost 全成了空值，且**不报错**。
REAL_USAGE_LINES = """\
Tokens: 3.2k sent, 2.4k cache hit, 97 received. Cost: $0.00050 message, $0.00050
session.
Applied edit to cart/pricing.py
Tokens: 3.4k sent, 2.4k cache hit, 2.5k received. Cost: $0.0038 message, $0.0043
session.
"""

#: 同一批里另外两种折法：一种在 `session.` 前面多个空格，一种把整个 `$0.00074`
#: 都推到了下一行。
#
#: 行尾那个空格写成 `\x20`，不是写成真的空格 —— aider 确实打了它，
#: 而 pre-commit 的 trim-trailing-whitespace 钩子会把源码里的真空格删掉，
#: 于是这份"逐字保留"的样本会在某次提交时被悄悄改掉，测试还照样绿。
REAL_USAGE_OTHER_WRAPS = (
    "Tokens: 3.3k sent, 2.4k cache hit, 607 received. Cost: $0.0012 message, $0.0012\x20\n"
    "session.\n"
    "Tokens: 3.2k sent, 2.4k cache hit, 293 received. Cost: $0.00074 message,\x20\n"
    "$0.00074 session.\n"
)

#: 一段跑成功时该有的输出。刷屏的中间过程照着真实运行的语气写，
#: 因为第 6 条契约要防的正是"用量行被淹在噪声里"。
SAMPLE_STDOUT = """\
Aider v0.86.2
Model: deepseek/deepseek-chat with diff edit format
Git repo: .git with 12 files
Repo-map: using 1024 tokens

auth/password.py
Add these files to the chat? y
Applied edit to auth/password.py
Tokens: 5.3k sent, 412 received. Cost: $0.0012 message, $0.0012 session.

Applied edit to auth/policy.py
Tokens: 6.1k sent, 233 received. Cost: $0.0009 message, $0.0021 session.
"""


# ── 数字怎么写的都要认 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("412", 412),
        ("5.3k", 5300),
        ("5.3K", 5300),
        ("1,024", 1024),
        ("2.1M", 2_100_000),
        (" 800 ", 800),
    ],
)
def test_token_counts_in_every_shape_aider_uses(raw: str, expected: int) -> None:
    """aider 为了好看会把大数写成 `5.3k`，直接 `int()` 会抛异常。"""
    assert parse_token_count(raw) == expected


# ── 用量汇总 ────────────────────────────────────────────────


def test_tokens_add_up_and_cost_takes_the_session_total() -> None:
    """token 逐轮累加，钱取最后一行的 session 值。

    `message` 是这一轮的钱，`session` 是从开跑到现在的累计。把 session 也加起来
    会得到一个成倍偏大的数字 —— 这里两行的 session 是 0.0012 和 0.0021，
    加起来 0.0033，而实际总花费就是 0.0021。
    """
    usage = parse_usage(SAMPLE_STDOUT)
    assert usage is not None
    assert usage.input_tokens == 5300 + 6100
    assert usage.output_tokens == 412 + 233
    assert usage.cost_usd == pytest.approx(0.0021)
    assert usage.turns == 2


def test_tokens_without_cost_are_still_reported() -> None:
    """litellm 不认识的模型没有价目表，这时它只打 token 不打钱。

    这种情况下 token 照报、cost 报"读不出来"。把 cost 填成 0 的话，
    成本统计会悄悄偏低，而且事后看不出是"真没花钱"还是"没读出来"。
    """
    usage = parse_usage("Tokens: 1.2k sent, 100 received.\n")
    assert usage is not None
    assert usage.input_tokens == 1200
    assert usage.cost_usd is None


def test_real_usage_lines_with_cache_hits_and_wrapping() -> None:
    """真实输出比编出来的样本多一个 `cache hit` 字段，而且会折行。

    2026-09-05 正式跑四道题时，一条都没解析出来，token 和 cost 全空 ——
    而这不会报错，只会让成本统计安静地变成一片空白。冒烟那次之所以没暴露，
    是因为冷启动没有缓存命中，格式恰好和我编的样本一样。
    """
    usage = parse_usage(REAL_USAGE_LINES)
    assert usage is not None
    assert usage.turns == 2
    assert usage.input_tokens == 3200 + 3400
    assert usage.output_tokens == 97 + 2500
    assert usage.cost_usd == pytest.approx(0.0043)


def test_cache_hits_are_recorded_but_not_added_to_the_total() -> None:
    """缓存命中是 `sent` 的一部分，不是另加的。

    加进总数的话 token 统计会凭空多出一截。单独记下来是有用的 ——
    DeepSeek 的缓存命中便宜一个数量级，不记的话没法解释
    "两次运行 token 差不多，钱差好几倍"。
    """
    usage = parse_usage(REAL_USAGE_LINES)
    assert usage is not None
    assert usage.cache_read_tokens == 2400 + 2400
    assert usage.input_tokens == 6600


@pytest.mark.parametrize("text", [REAL_USAGE_LINES, REAL_USAGE_OTHER_WRAPS])
def test_every_wrap_position_seen_in_the_wild_parses(text: str) -> None:
    """折在哪儿取决于消息有多长，没有规律。三种真实劈法都得认。"""
    usage = parse_usage(text)
    assert usage is not None
    assert usage.turns == 2
    assert usage.cost_usd is not None


def test_no_usage_line_at_all_returns_none() -> None:
    """一行都没匹配上就是"读不出来"，不能返回一个全 0 的对象冒充。"""
    assert parse_usage("Aider v0.86.2\n什么都没干就退出了\n") is None


def test_usage_survives_a_screenful_of_noise() -> None:
    """真实运行时用量行会被埋在几百行输出中间。"""
    noisy = "\n".join([f"[info] 正在读第 {i} 个文件" for i in range(300)]) + "\n" + SAMPLE_STDOUT
    usage = parse_usage(noisy)
    assert usage is not None
    assert usage.turns == 2


# ── 版本 ────────────────────────────────────────────────────


def test_version_comes_from_the_banner() -> None:
    """版本以现场为准，不以代码里那个常量为准 —— 镜像和常量可能不同步。"""
    assert parse_version(SAMPLE_STDOUT) == "0.86.2"


def test_missing_banner_is_not_an_error() -> None:
    assert parse_version("直接就开始干活了\n") is None


# ── 鉴权 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "litellm.AuthenticationError: ...",
        "Error code: 401 - {'error': ...}",
        "Incorrect API key provided",
        "No API key found for deepseek",
    ],
)
def test_auth_failures_are_recognised(text: str) -> None:
    """判错的代价不对称：鉴权失败按 C-18 重试 3 次，运行时错误只重试 1 次。

    把鉴权失败当成运行时错误，一个配错的 Key 会安静地把解决率拉到 0。
    """
    assert looks_like_auth_failure(text)


def test_auth_failure_survives_aider_hard_wrapping() -> None:
    """这段是 2026-09-05 第一次真跑抓下来的原文，一个字没改。

    aider 按终端宽度硬折行，而且**从单词中间折** —— 这里
    `invalid_request_error` 被劈成了 `erro` 和 `r`，`Authentication Fails`
    被折成两行。照原样做子串匹配的话，认不认得出来取决于消息恰好有多长。
    """
    assert looks_like_auth_failure(REAL_AUTH_FAILURE_STDOUT)
    assert has_model_side_failure(REAL_AUTH_FAILURE_STDOUT)


def test_a_normal_run_is_not_mistaken_for_an_auth_failure() -> None:
    assert not looks_like_auth_failure(SAMPLE_STDOUT)
    assert not has_model_side_failure(SAMPLE_STDOUT)


def test_a_token_count_containing_401_is_not_an_auth_failure() -> None:
    """`Tokens: 1401 sent` 里有 `401`。

    早先那版把 `"401"` 直接写进标记清单，这一行会让一次正常的运行被判成鉴权失败，
    然后按 C-18 白重试三次。
    """
    assert not looks_like_auth_failure("Tokens: 1401 sent, 1401 received.")


# ── 轨迹 ────────────────────────────────────────────────────


def test_trajectory_records_usage_and_edits_only() -> None:
    """只记两类**确定能对上**的事件：每轮用量，和每次落地的编辑。

    不去猜自然语言里哪句是"思考"。轨迹会被当成失败归因的证据，猜错比没有更糟。
    """
    lines = build_trajectory(SAMPLE_STDOUT, started_at=datetime.now(UTC)).splitlines()
    assert len(lines) == 4
    kinds = [line.split('"type": "')[1].split('"')[0] for line in lines]
    assert kinds.count("llm_usage") == 2
    assert kinds.count("tool_call") == 2
    assert "auth/password.py" in lines[2]


def test_trajectory_of_a_silent_run_is_empty() -> None:
    """什么都没解析出来时给空串，让上层不要存一个 0 字节的制品。"""
    assert build_trajectory("什么都没有\n", started_at=datetime.now(UTC)) == ""


# ── 提示词 ──────────────────────────────────────────────────


def test_message_carries_the_issue_and_the_generic_protected_list() -> None:
    """题干原样带上，受保护清单用任务输入里那份。

    **不要自己另拼一份清单。** `task.constraints.protected_paths` 是
    `agent_visible_patterns()` 的产物（只有通用规则）；含该题
    `test_patch_paths` 的那份是给平台执行过滤用的，下发出去等于告诉 AI
    官方改了哪几个文件（协议 C-76）。
    """
    task = make_task_input(deadline_ms=1)
    message = build_message(task)
    assert task.issue.title in message
    assert task.issue.body in message
    for pattern in task.constraints.protected_paths:
        assert pattern in message


def test_message_follows_the_issue_language() -> None:
    """中文题干配中文指令。混着写的话模型有时会跟着指令切语言，日志会变得难读。"""
    task = make_task_input(deadline_ms=1)
    assert task.issue.language is IssueLanguage.ZH
    assert "只改产品代码" in build_message(task)

    english = task.model_copy(update={"issue": task.issue.model_copy(update={"language": "en"})})
    assert "Only change production code" in build_message(english)


# ── 命令 ────────────────────────────────────────────────────


def test_command_carries_the_switches_that_must_not_be_missed() -> None:
    """五个开关少一个的后果各不相同，都在模块开头那张表里。

    `--no-gitignore` 最不显眼也最坑：不给的话 aider 会往仓库的 `.gitignore` 里
    加一行 `.aider*`，那会变成补丁里的一处改动，然后被算进"AI 改了几个文件"。
    """
    command = build_command(make_task_input(deadline_ms=1), "deepseek/deepseek-chat")
    for flag in (
        "--yes-always",
        "--no-auto-commits",
        "--no-gitignore",
        "--no-pretty",
        "--no-check-update",
    ):
        assert flag in command
    assert command[:3] == ["aider", "--model", "deepseek/deepseek-chat"]


def test_the_prompt_is_one_argv_element() -> None:
    """题干里带引号、反引号、`$` 的情况多得是。

    命令走的是列表不是字符串，所以整段提示词必须是**一个**参数 ——
    拆开或者拼成 shell 字符串，一个反引号就能改变命令的含义。
    """
    command = build_command(make_task_input(deadline_ms=1), "m")
    assert command[-2] == "--message"
    assert command.count("--message") == 1
    assert command[-1] == build_message(make_task_input(deadline_ms=1))


def test_extra_args_land_before_the_message() -> None:
    """`agent_configs.params` 里追加的参数要在 `--message` 之前，不能挤到题干后面。"""
    command = build_command(make_task_input(deadline_ms=1), "m", extra_args=("--map-tokens", "0"))
    assert command.index("--map-tokens") < command.index("--message")
