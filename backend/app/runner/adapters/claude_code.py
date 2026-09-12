"""Claude Code 适配器：第二个真实的被测 AI（E3-T5）。

和 Aider 走的是同一条路 —— 在容器里改 `/workspace` 下的文件，补丁由我们跑
`git diff` 生成（`04-runner-protocol.md` §9.1 的 workspace-mutation 模式）。
不同的地方只有一处，但很关键：**它的输出是 JSONL，不是自然语言。**

## 为什么这件事值得单独说

Aider 那边我们拿正则去 stdout 里抠 `Tokens: 5.3k sent, 412 received.`，
折行、缓存命中、终端宽度全要防，格式一变就悄悄变成空值（2026-09-05 真踩过）。
Claude Code 有 `--output-format stream-json`：每行一个 JSON 事件，
最后一行 `result` 事件直接带 `num_turns`、`usage`、`total_cost_usd`、`is_error`。

顺带把 E3-T5 多出来的那条验收标准也解决了 —— **轨迹能还原工具调用序列**。
Aider 只能从 `Applied edit to <path>` 认出"改了文件"，别的一概不知；
这里 `assistant` 消息里带 `tool_use` 块，`Read` / `Edit` / `Bash` / `Grep`
连名字带参数都是 CLI 自己标好的，不用猜。

## 四个必须给的开关

| 参数 | 不给会怎样 |
|:---|:---|
| `--print` | 它进交互模式，容器一直挂到超时 |
| `--output-format stream-json` | 只能拿到一段自然语言，token 和 cost 全没了 |
| `--verbose` | **这个格式必须配它**，不给的话 CLI 直接拒绝启动 |
| `--permission-mode bypassPermissions` | 每次改文件都停下来问人，同样挂到超时 |

`--max-turns` 是任务卡点名的预算闸门，也是 Aider 没有的东西（那边只能靠墙钟）。

## 两个会安静出错的坑

**① token 的口径和 Aider 反着。** Anthropic 报文里的 `input_tokens`
**不含**缓存那部分，缓存另外用 `cache_read_input_tokens` /
`cache_creation_input_tokens` 两个字段报；而 DeepSeek 那边（Aider 读到的）
`sent` 是含缓存的总数。平台这边的口径跟后者一致 ——
`cache_read` 是 `input` 的一部分，不是另加的（见迁移 `0003` 的说明）。
所以这里必须把三个数加起来当 `input`，直接把 `input_tokens` 传下去的话，
一次命中缓存的运行会少报九成的输入量，按 token 估的成本跟着一起偏低。

**② `total_cost_usd` 是 CLI 自己按价目表算的，不是服务端返回的。**
走中转端点、模型名它不认识时，这个数很可能就是 0 —— 而 0 正是协议纪律 3
点名不许填的那个值（"报不出来"和"真没花钱"在报表里待遇完全不同）。
所以规则是：**只有直连官方端点才信它，配了 `base_url` 一律报 `unavailable`**，
让平台按 token 去估并标记成 `estimated`。

## 凭据怎么进容器

`Settings.agent_env_for()` 按模型名挑出该家的 Key（比如
`deepseek-chat` → `DEEPSEEK_API_KEY`），但 claude 这个 CLI 只认
`ANTHROPIC_*` 那几个名字。这层改名是**适配器的知识**，不是配置的知识，
所以放在这里做，见 `credential_env()`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import CostSource
from app.infrastructure.logging import get_logger
from app.runner.adapters.cli_text import failure_excerpt, looks_like_auth_failure
from app.runner.adapters.prompt import build_task_prompt
from app.runner.patch import capture_workspace_diff
from app.runner.protocol import (
    AGENT_STDERR_FILENAME,
    AGENT_STDOUT_FILENAME,
    AGENT_TRAJECTORY_FILENAME,
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    OOM_KILLED,
    RUNTIME_ERROR,
    AgentConfig,
    AgentError,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
    TokenUsage,
)
from app.sandbox.container import (
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    Stage,
    agent_limits,
    get_docker_client,
)
from app.sandbox.container import run_in_container as _run_in_container

logger = get_logger(__name__)

#: 装了 claude 的镜像。和 `images/claude-code/Dockerfile`、Makefile 的
#: `CLAUDE_CODE_IMAGE` 对齐。只是默认值 —— 真实评测由
#: `agent_configs.params["image"]` 决定。
DEFAULT_CLAUDE_CODE_IMAGE = "bench-agent:py311-claude-code"

#: 镜像里钉死的版本。取的是 npm 上的 `stable` 标签而不是 `latest`：
#: 这个包一天能发好几个版本（查的时候 507 个版本、latest 2.1.263、stable 2.1.236），
#: 评测平台要的是"半年后重跑还是这个数"，不是最新。
#:
#: 只在 stdout 的 init 事件里读不到版本时兜底，读得到就以现场为准。
PINNED_CLAUDE_CODE_VERSION = "2.1.236"

#: 默认给几轮预算。40 轮够一道 Golden 题来回读几个文件再改几处；
#: 给太小会把"还没想完"记成"没修好"，给太大则是拿钱换一个不会发生的长尾。
#: 真实评测由 `agent_configs.params["max_turns"]` 决定。
DEFAULT_MAX_TURNS = 40

#: 容器超时后留给它落盘的宽限期（秒）。和 Aider 用同一个数，
#: 理由也一样：协议 C-09a 要求超时也要保存补丁。
CLAUDE_STOP_GRACE_S = 20

#: `run()` 至少要有这么多秒才值得起容器。低于它直接按截止已过返回 ——
#: 起容器、拉起 node、装载 CLI 本身就要十几秒。
MIN_USEFUL_SECONDS = 30

#: 轨迹里一条 `summary` / `text_excerpt` 最多留多长。轨迹是给人看的证据，
#: 不是日志备份 —— 全量 stdout 另存一份（§9.5 最后一句）。
SUMMARY_CHARS = 200
TEXT_EXCERPT_CHARS = 500

#: claude 这个 CLI 认的环境变量名。**改名映射只在这里做一次。**
ANTHROPIC_KEY_VAR = "ANTHROPIC_API_KEY"
ANTHROPIC_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_BASE_URL_VAR = "ANTHROPIC_BASE_URL"

#: 这几个名字进容器之前都要在 `AGENT_ENV_ALLOWLIST` 里。
#: 有一条单测盯着这件事 —— 加一个新名字忘了加白名单的话，
#: 表现是容器里少一个变量，而 CLI 只会说"没有 API Key"。
CREDENTIAL_VARS: tuple[str, ...] = (ANTHROPIC_KEY_VAR, ANTHROPIC_TOKEN_VAR, ANTHROPIC_BASE_URL_VAR)

#: 从工具参数里挑一个字段当摘要，按这个顺序找第一个有值的。
#: 不把整个 `input` 写进轨迹：`Edit` 的参数里是整段旧文本和新文本，
#: 写进去等于把补丁又存了一遍，轨迹会大到没法看。原样留一个 sha256 就够对账了。
SUMMARY_KEYS: tuple[str, ...] = (
    "file_path",
    "notebook_path",
    "path",
    "command",
    "pattern",
    "query",
    "url",
    "description",
)


@dataclass(frozen=True, slots=True)
class ClaudeUsage:
    """从 stream-json 里汇总出来的用量。

    `cost_usd` 为 None 表示**不信任或读不出来**，不是 0（协议纪律 3）。
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float | None
    #: CLI 自己数的轮数。用它而不是"我们数了几次 API 调用"，是为了和
    #: `--max-turns` 同一个刻度 —— 两个刻度混着用的话，
    #: "这次是不是把预算用完了"就没法回答了。
    turns: int


# ── 解析 ────────────────────────────────────────────────────


def parse_events(stdout: str) -> list[dict[str, Any]]:
    """把 stdout 切成一串事件。不是 JSON 的行直接跳过。

    为什么要容忍杂行：协议纪律 1 就写着"前面允许任意日志，真实 CLI 一定会刷屏"。
    node 的告警、代理的提示都可能混进来。还有一种情况更要紧 ——
    **容器被超时杀掉时，最后一行往往只写了一半**，那一行必须能安静地丢掉，
    否则一次超时（协议要求照样保存补丁）会变成一次解析崩溃。
    """
    events: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def result_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """最后那个 `result` 事件。没有就返回 None（多半是被杀在半路）。"""
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return None


def init_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """开头那个 `system`/`init` 事件。里面有 CLI 版本、真正用上的模型、工具清单。"""
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event
    return None


def _message_of(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    message = event.get("message")
    return message if isinstance(message, Mapping) else None


def _blocks_of(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _sum_usage(raw: Mapping[str, Any]) -> tuple[int, int, int]:
    """一份 usage 报文 →（输入、输出、缓存读）。

    输入要把三个字段加起来，理由见模块开头第 ① 条：Anthropic 的
    `input_tokens` 不含缓存，而平台的口径是 `cache_read ⊆ input`。
    `cache_creation` 也算输入 —— 那些 token 确实发出去了，只是顺带写进了缓存。
    """

    def number(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) else 0

    cache_read = number("cache_read_input_tokens")
    total_input = number("input_tokens") + number("cache_creation_input_tokens") + cache_read
    return total_input, number("output_tokens"), cache_read


def per_call_usage(events: list[dict[str, Any]]) -> list[Mapping[str, Any]]:
    """每次 API 调用的用量，**按 `message.id` 去重**。

    2026-09-06 实测踩到的：一次调用会发出**多条** `assistant` 事件
    （思考一条、工具调用一条），而且每条都带着**同一份** usage。
    照事件求和的话，一次三轮的运行输入 token 从 20019 变成 40038 ——
    正好翻倍，而且不会报任何错，只会让成本统计凭空贵一倍。

    录下来的那份真实报文在 `tests/fixtures/claude_code/`。
    """
    seen: dict[str, Mapping[str, Any]] = {}
    anonymous: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = _message_of(event)
        if message is None:
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            seen.setdefault(message_id, usage)
        else:
            # 没有 id 就只能照收。宁可多算也不漏 —— 漏了看不出来，多了至少数字异常
            anonymous.append(usage)
    return [*seen.values(), *anonymous]


def parse_usage(events: list[dict[str, Any]], *, trust_cost: bool) -> ClaudeUsage | None:
    """汇总 token / cost / 轮数。什么都读不到返回 None。

    **`result` 事件里那份 usage 才是权威。** 一开始我写的是"自己逐条累加"
    （Aider 那边就是这么干的），2026-09-06 实测把这个想法否掉了两次：
    逐条会重复计数（见 `per_call_usage`），而且**每条消息的 `output_tokens`
    都是 0**，真实的输出量只出现在 `result` 里。

    逐条求和留作兜底，因为**被杀在半路时根本没有 `result` 事件** ——
    协议 C-09a 要求超时也要留下证据，光靠 result 的话那些运行会一片空白。
    这条路上输出量可能是 0，那是如实反映"这份报文里只有这些"，不是我们算错了。

    `trust_cost=False`（配了 `base_url`）时不取 cost，理由见模块开头第 ② 条。
    """
    final = result_event(events)
    calls = per_call_usage(events)

    if final is not None and isinstance(final.get("usage"), Mapping):
        inputs, outputs, cache_reads = _sum_usage(final["usage"])
    elif calls:
        inputs = outputs = cache_reads = 0
        for usage in calls:
            got_input, got_output, got_cache = _sum_usage(usage)
            inputs += got_input
            outputs += got_output
            cache_reads += got_cache
    else:
        return None

    cost: float | None = None
    if trust_cost and final is not None and isinstance(final.get("total_cost_usd"), int | float):
        cost = float(final["total_cost_usd"])

    turns = final.get("num_turns") if final else None
    return ClaudeUsage(
        input_tokens=inputs,
        output_tokens=outputs,
        cache_read_tokens=cache_reads,
        cost_usd=cost,
        # 没有 result 事件时退回"数了几次 API 调用"。它和 num_turns 不是同一个刻度
        # （后者把工具往返也算上），但比留空强 —— 留空的话超时的运行看不出跑了多久
        turns=int(turns) if isinstance(turns, int) else len(calls),
    )


#: init 事件里放 CLI 版本的字段名。**是 `claude_code_version`，不是 `version`。**
#: 2026-09-06 实测：原本按 `version` 读，一直读不到，于是每次都退回镜像里钉的那个
#: 常量 —— 而常量和现场版本可能不同步，报表上就会写着一个没跑过的版本号。
#: 这类错误不会报错，只会让"哪个版本跑出来的"这件事变得不可信。
VERSION_KEYS: tuple[str, ...] = ("claude_code_version", "version")


def parse_version(events: list[dict[str, Any]]) -> str | None:
    """从 init 事件里读 CLI 版本。读不到返回 None（由调用方退回钉死的常量）。"""
    init = init_event(events)
    if init is None:
        return None
    for key in VERSION_KEYS:
        value = init.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ── 轨迹 ────────────────────────────────────────────────────


def args_digest(args: Mapping[str, Any]) -> str:
    """工具参数的指纹。轨迹里只留它，不留原文（理由见 `SUMMARY_KEYS`）。"""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tool_summary(args: Mapping[str, Any]) -> str:
    """一句话说清这次工具调用干了什么。

    容器里的路径都带 `/workspace/` 前缀，这里剥掉 —— 轨迹是给人看的，
    满屏的绝对路径只会把真正有用的那截文件名挤出视野。
    """
    for key in SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            prefix = WORKSPACE_TARGET.rstrip("/") + "/"
            if text.startswith(prefix):
                text = text[len(prefix) :]
            return text[:SUMMARY_CHARS]
    return ""


def tool_outcomes(events: list[dict[str, Any]]) -> dict[str, bool]:
    """每次工具调用成没成功：`tool_use_id` → 有没有报错。

    结果在后面的 `user` 事件里，按 id 回填到 `tool_call` 上，
    这样轨迹仍然是一行一次调用，而"哪一步失败了"看得见 —— 失败归因（E6）要用。
    """
    outcomes: dict[str, bool] = {}
    for event in events:
        if event.get("type") != "user":
            continue
        message = _message_of(event)
        if message is None:
            continue
        for block in _blocks_of(message):
            if block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            if isinstance(tool_id, str):
                outcomes[tool_id] = not bool(block.get("is_error"))
    return outcomes


def build_trajectory(events: list[dict[str, Any]], *, started_at: datetime) -> str:
    """把事件流转成 §9.5 的 JSONL 轨迹，每行一个事件。

    三类事件这次**全都落地**（Aider 那边只有两类）：`tool_call` 来自
    `tool_use` 块、`message` 来自 `text` 块、`llm_usage` 来自每条消息的 usage。
    `message` 这次敢写，是因为角色是 CLI 自己标的，不是我们从自然语言里猜的 ——
    E3-T4 拒绝写 `message` 事件针对的正是"猜"。

    **`ts` 全都是开跑时刻，真正的顺序看 `seq`。** stream-json 的事件本身不带
    时间戳，而我们是等容器结束之后才读的这段 stdout，编不出每一步的真实时刻。
    与其填一串看着像真的、其实是插值出来的时间，不如老实标一个序号 ——
    轨迹会被当成归因证据用，编出来的证据比没有更糟。
    """
    ts = int(started_at.timestamp() * 1000)
    outcomes = tool_outcomes(events)
    lines: list[str] = []

    def emit(event: dict[str, Any]) -> None:
        lines.append(json.dumps({"ts": ts, "seq": len(lines), **event}, ensure_ascii=False) + "\n")

    for event in events:
        if event.get("type") != "assistant":
            continue
        message = _message_of(event)
        if message is None:
            continue
        for block in _blocks_of(message):
            kind = block.get("type")
            if kind in ("text", "thinking"):
                # `thinking` 是模型的推理过程，CLI 自己标好的类型 —— 不是我们从
                # 自然语言里猜的，所以敢落地。归因（E6）要回答"它为什么改这里"，
                # 这段是最直接的证据。标一个 thinking 标记，别和结论混在一起
                text = str(block.get(kind) or "").strip()
                if text:
                    entry: dict[str, Any] = {
                        "type": "message",
                        "role": "assistant",
                        "text_excerpt": text[:TEXT_EXCERPT_CHARS],
                    }
                    if kind == "thinking":
                        entry["thinking"] = True
                    emit(entry)
            elif kind == "tool_use":
                args = block.get("input")
                args = args if isinstance(args, Mapping) else {}
                tool_id = block.get("id")
                call: dict[str, Any] = {
                    "type": "tool_call",
                    "name": str(block.get("name") or "unknown"),
                    "args_digest": args_digest(args),
                    "summary": tool_summary(args),
                }
                if isinstance(tool_id, str) and tool_id in outcomes:
                    call["ok"] = outcomes[tool_id]
                emit(call)
        usage = message.get("usage")
        if isinstance(usage, Mapping):
            got_input, got_output, _ = _sum_usage(usage)
            emit({"type": "llm_usage", "input": got_input, "output": got_output})
    return "".join(lines)


# ── 命令与凭据 ──────────────────────────────────────────────


def build_command(
    task: AgentTaskInput,
    model: str,
    *,
    max_turns: int,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """拼容器里要跑的那条命令。每个开关为什么必须给，见模块开头那张表。

    **提示词放在最后当位置参数。** 夹在中间的话，题干里但凡以 `-` 开头的一行
    都可能被当成新的开关。用列表不用字符串同理：题干里带引号、反引号、`$`
    的情况多得是，走 shell 会改变命令的含义。
    """
    return [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--max-turns",
        str(max_turns),
        "--model",
        model,
        *extra_args,
        build_task_prompt(task),
    ]


def credential_env(env: Mapping[str, str], *, base_url: str | None) -> dict[str, str]:
    """把 harness 挑好的那把 Key 翻译成 claude 认识的名字。

    直连官方端点（`base_url` 为空）时**什么都不做** —— `agent_env_for()`
    给的就是 `ANTHROPIC_API_KEY`，名字已经对上了。

    配了 `base_url` 就是走中转/兼容端点（比如
    `https://api.deepseek.com/anthropic`）。这时 `agent_env_for()` 挑出来的是
    那一家的 Key（`DEEPSEEK_API_KEY`），而 claude 只认 `ANTHROPIC_AUTH_TOKEN`，
    所以这里换个名字装进去。

    **换完之后原来那把 Key 就不留在容器里了。** 是同一个字符串，但被测对象是一个
    会执行任意代码的 AI，多一个变量就多一个它能读到的东西，而这换不到任何好处 ——
    和 `Settings.agent_env_for()` 里"只给这一家的 Key"是同一条纪律。

    一把 Key 都找不到时不报错、不猜：让 claude 自己去报"没有凭据"，
    那条报错会被 `looks_like_auth_failure()` 认出来，归成 `AGENT_AUTH_ERROR`，
    日志里看得见。这里静默失败反而查不出来。
    """
    result = {name: value for name, value in env.items() if not _is_credential(name)}
    if base_url is None:
        token = env.get(ANTHROPIC_KEY_VAR)
        if token:
            result[ANTHROPIC_KEY_VAR] = token
        return result

    result[ANTHROPIC_BASE_URL_VAR] = base_url
    token = env.get(ANTHROPIC_TOKEN_VAR) or env.get(ANTHROPIC_KEY_VAR) or _any_api_key(env)
    if token:
        result[ANTHROPIC_TOKEN_VAR] = token
    return result


def _is_credential(name: str) -> bool:
    return name.endswith(("_API_KEY", "_AUTH_TOKEN")) or name == ANTHROPIC_BASE_URL_VAR


def _any_api_key(env: Mapping[str, str]) -> str | None:
    """随便哪一家的 Key。`agent_env_for()` 一次只给一把，所以这里最多就一个。"""
    for name in sorted(env):
        if name.endswith("_API_KEY") and env[name]:
            return env[name]
    return None


# ── 适配器 ──────────────────────────────────────────────────


class ClaudeCodeRunner:
    """在容器里跑 Claude Code，跑完从工作区抓补丁。

    构造参数在真实评测下只有 `base_url` 和 `max_turns` 需要给，
    而且是由 `from_params()` 从 `agent_configs.params` 读出来的，不用手写。
    `model` 只给契约测试和命令行冒烟用 —— **生产不要传**，传了就等于绕过数据库里
    那份配置，报表上写的模型和实际用的会对不上。
    """

    name = "claude-code"

    def __init__(
        self,
        *,
        image: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        run_container: Any = None,
    ) -> None:
        self._image = image
        self._model = model
        self._base_url = base_url or None
        self._max_turns = max(1, int(max_turns))
        #: 测试用的接缝：传一个假的进来，不起容器也能验命令拼装和故障映射。
        self._run_container = run_container or _run_in_container

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> ClaudeCodeRunner:
        """按 `agent_configs.params` 造一个。编排层走的就是这条路。

        `image` 不在这里读 —— 它由 `AgentConfig.image` 送进来（编排层已经从同一份
        params 里取过了）。两处都读的话，改一处忘另一处就会出现"报表说用镜像 A、
        实际跑的是 B"，而这种不一致从数据上看不出来。
        """
        raw_turns = params.get("max_turns", DEFAULT_MAX_TURNS)
        try:
            max_turns = int(raw_turns)
        except (TypeError, ValueError):
            logger.warning("max_turns 不是数字，退回默认值", value=repr(raw_turns))
            max_turns = DEFAULT_MAX_TURNS
        base_url = params.get("base_url")
        return cls(base_url=str(base_url) if base_url else None, max_turns=max_turns)

    # ── 探活 ────────────────────────────────────────────────

    def probe(self) -> ProbeResult:
        """看镜像在不在。**不起容器、不调模型、不花钱。**

        探不到凭据是有意的：Key 是 `AgentConfig.env` 的内容，那是每次运行才拼出来的，
        探活拿不到。真正能在第一秒挡住过期 Key 的是 E5 编排层的开跑前检查，
        这里假装能探反而会给出虚假的安心。
        """
        image = self._image or DEFAULT_CLAUDE_CODE_IMAGE
        try:
            get_docker_client().images.get(image)
        except Exception as exc:
            return ProbeResult(ok=False, detail=f"镜像 {image} 不可用：{type(exc).__name__}: {exc}")
        endpoint = self._base_url or "官方端点"
        return ProbeResult(
            ok=True,
            agent_version=PINNED_CLAUDE_CODE_VERSION,
            detail=f"镜像 {image} 就绪；端点 {endpoint}；模型由每次运行的任务输入决定",
        )

    # ── 干活 ────────────────────────────────────────────────

    def run(self, task: AgentTaskInput, workspace: Any, config: AgentConfig) -> AgentRunResult:
        """跑一道题：起容器改工作区 → `git diff` 抓补丁 → 读 stream-json。"""
        started_at = datetime.now(UTC)
        remaining_s = (task.constraints.deadline_unix_ms - _now_ms()) / 1000.0

        # 截止已经过了就别起容器了。契约第 3 条要的"优雅返回、不留孤儿进程"，
        # 最干净的实现方式就是根本没起过任何东西
        if remaining_s < MIN_USEFUL_SECONDS:
            return self._result(
                started_at=started_at,
                model=self._model_for(task),
                exit_code=0,
                error=AgentError(
                    code=DEADLINE_EXCEEDED,
                    message=f"截止时刻只剩 {remaining_s:.1f} 秒，不足以起一个容器，直接收手",
                ),
            )

        spec = self._spec(task, workspace, config, timeout_s=int(remaining_s))
        container = self._run_container(spec)
        events = parse_events(container.stdout)
        self._dump_side_files(config, container, events, started_at)

        # 补丁在容器结束之后才抓，超时被杀也照抓 —— 协议 C-09a：超时也要保存补丁。
        # 抓的是**原始** diff，受保护路径的改动留着，过滤是平台的事（C-08b）
        patch = capture_workspace_diff(workspace)
        usage = parse_usage(events, trust_cost=self._base_url is None)

        return self._result(
            started_at=started_at,
            model=self._model_for(task),
            version=parse_version(events) or PINNED_CLAUDE_CODE_VERSION,
            exit_code=container.exit_code,
            patch=patch,
            usage=usage,
            error=_error_for(container, result_event(events)),
            raw_stdout_bytes=len(container.stdout.encode("utf-8")),
            raw_stderr_bytes=len(container.stderr.encode("utf-8")),
            trajectory_uri=_trajectory_uri(config),
        )

    # ── 内部 ────────────────────────────────────────────────

    def _model_for(self, task: AgentTaskInput) -> str:
        """这次用哪个模型。构造参数优先，只为契约测试和冒烟准备，生产走任务输入。"""
        return self._model or task.model.name

    def _spec(
        self, task: AgentTaskInput, workspace: Any, config: AgentConfig, *, timeout_s: int
    ) -> ContainerSpec:
        return ContainerSpec(
            image=config.image or self._image or DEFAULT_CLAUDE_CODE_IMAGE,
            command=build_command(
                task,
                self._model_for(task),
                max_turns=self._max_turns,
                extra_args=config.extra_args,
            ),
            timeout_s=timeout_s,
            stage=Stage.AGENT,
            # 被测 AI 要连大模型 API，所以 Agent 阶段是联网的。测试阶段永远断网（C-31），
            # 那由测试执行器自己保证，两边互不影响
            network=NetworkMode.BRIDGE if task.constraints.allow_network else NetworkMode.NONE,
            mounts=(BindMount.workspace(Path(workspace.path)),),
            workdir=WORKSPACE_TARGET,
            # 限额显式给（E9-T2），理由同 aider.py
            limits=agent_limits(cpus=config.cpus, memory_mb=config.memory_mb),
            env=credential_env(config.env, base_url=self._base_url),
            stop_grace_s=CLAUDE_STOP_GRACE_S,
            run_id=task.task_id,
        )

    def _dump_side_files(
        self,
        config: AgentConfig,
        container: ContainerResult,
        events: list[dict[str, Any]],
        started_at: datetime,
    ) -> None:
        """把全量 stdout/stderr 和轨迹写到 `config.artifact_dir`，交给上层去存。

        写文件失败只记日志：制品是证据，丢了很可惜，但为它把一次真实的评测结果
        作废是本末倒置。
        """
        directory = config.artifact_dir
        if directory is None:
            return
        files = {
            AGENT_STDOUT_FILENAME: container.stdout,
            AGENT_STDERR_FILENAME: container.stderr,
            AGENT_TRAJECTORY_FILENAME: build_trajectory(events, started_at=started_at),
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for filename, text in files.items():
                if text:
                    (directory / filename).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Agent 侧制品落盘失败", directory=str(directory), error=str(exc))

    def _result(
        self,
        *,
        started_at: datetime,
        model: str,
        exit_code: int,
        version: str = PINNED_CLAUDE_CODE_VERSION,
        patch: str = "",
        usage: ClaudeUsage | None = None,
        error: AgentError | None = None,
        raw_stdout_bytes: int = 0,
        raw_stderr_bytes: int = 0,
        trajectory_uri: str | None = None,
    ) -> AgentRunResult:
        """按同一套规则组装结果。

        成本那三行是这里最要紧的部分：**读不出来（或者不该信）就报 `unavailable`
        并把 `cost_usd` 留成 None**，不许拿 0 顶替（协议纪律 3，契约第 5 条）。
        """
        finished_at = datetime.now(UTC)
        token_usage = None
        if usage is not None:
            token_usage = TokenUsage(
                input=usage.input_tokens,
                output=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
                # 缓存读已经算在 input 里了，不能再加一遍
                total=usage.input_tokens + usage.output_tokens,
            )
        cost = usage.cost_usd if usage is not None else None
        return AgentRunResult(
            agent_name=self.name,
            agent_version=version,
            model=model,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            exit_code=exit_code,
            patch=patch,
            # 补丁是我们跑 git diff 生成的，不是 AI 在 stdout 里打印的。
            # 这个字段是归因用的元数据：后者的行号常写错，排查时要能分开看
            patch_source="git_diff",
            token_usage=token_usage,
            cost_usd=cost,
            cost_source=CostSource.REPORTED if cost is not None else CostSource.UNAVAILABLE,
            turns=usage.turns if usage is not None else None,
            trajectory_uri=trajectory_uri,
            error=error,
            raw_stdout_bytes=raw_stdout_bytes,
            raw_stderr_bytes=raw_stderr_bytes,
        )


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _trajectory_uri(config: AgentConfig) -> str | None:
    if config.artifact_dir is None:
        return None
    return (config.artifact_dir / AGENT_TRAJECTORY_FILENAME).as_uri()


def _error_for(container: ContainerResult, result: Mapping[str, Any] | None) -> AgentError | None:
    """容器怎么结束的 → 适配器自报的错误码（`_AGENT_ERROR_TO_INFRA` 会查这个码）。

    **顺序不能换**：先看 OOM 再看超时。两种情况的退出码都是 137，反过来判会把
    内存超限当成超时 —— 前者按 C-18 要降配重试，后者直接判 AI 没修好（协议 C-19b）。

    **轮数用完不算故障。** `subtype == "error_max_turns"` 时 `is_error` 是 true、
    退出码是 1，照字面判就成了"平台/AI 崩了"，于是触发重试、白花一次钱，
    归因也会指向错误的方向。它真正的含义是"在给定预算内没修完"，
    和"改了但没改对"是同一类结果，该交给 Judge 去判 —— 证据在 `turns` 那一列，
    它等于 `--max-turns` 就是用满了。

    反过来，**`result` 事件缺席要当故障**。正常跑完一定有这一行；没有它说明
    CLI 崩在了半路（或者 `--output-format` 没生效），这时哪怕退出码是 0，
    也不能把"工作区没改动"记成"AI 没修好"。
    """
    if container.oom_killed:
        return AgentError(code=OOM_KILLED, message="Agent 容器内存超限被杀")
    if container.timed_out:
        return AgentError(code=DEADLINE_EXCEEDED, message="Agent 容器墙钟超时被杀")

    if result is not None and result.get("subtype") == "error_max_turns":
        return None
    if result is not None and not result.get("is_error") and container.exit_code == 0:
        return None

    excerpt = failure_excerpt(container)
    text = container.stdout + "\n" + container.stderr
    if looks_like_auth_failure(text):
        return AgentError(code=AUTH_FAILED, message=f"疑似鉴权失败：{excerpt}")
    if result is None:
        return AgentError(
            code=RUNTIME_ERROR,
            message=f"stdout 里没有 result 事件，claude 多半崩在半路（退出码 "
            f"{container.exit_code}）：{excerpt}",
        )
    return AgentError(
        code=RUNTIME_ERROR,
        message=f"claude 失败（{result.get('subtype') or '未知'}，退出码 "
        f"{container.exit_code}）：{excerpt}",
    )


__all__ = [
    "CREDENTIAL_VARS",
    "DEFAULT_CLAUDE_CODE_IMAGE",
    "DEFAULT_MAX_TURNS",
    "PINNED_CLAUDE_CODE_VERSION",
    "ClaudeCodeRunner",
    "ClaudeUsage",
    "args_digest",
    "build_command",
    "build_trajectory",
    "credential_env",
    "init_event",
    "parse_events",
    "parse_usage",
    "parse_version",
    "per_call_usage",
    "result_event",
    "tool_outcomes",
    "tool_summary",
]
