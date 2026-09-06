"""平台和被测 AI 之间的接口（E3-T1，`docs/plan/04-runner-protocol.md` §9.2、§9.3）。

一句话：**标准输入喂一行 JSON 任务，标准输出的最后一行返回一行 JSON 结果，
结果里带一段 unified diff。**

## 协议的边界在适配器上，不在 AI 上

Claude Code、Aider、Qwen Code 这些工具没有一个是"从 stdin 读任务、往 stdout 打印
补丁"的用法——它们直接改工作目录里的文件，stdout 打印的是干活过程的自然语言。
硬要求它们最后打印一段 diff 极不稳定：会截断、会带 markdown 围栏、会写错行号。

所以协议主体是**适配器进程**（§9.1）：

    harness ──stdin(AgentTaskInput)──▶ [适配器] ──stdout(AgentRunResult)──▶ harness
                                          │
                                          ├─ 调真实 CLI 改写工作区，然后 git diff → patch
                                          └─ 或者 AI 自己产出 diff（strict 模式）→ patch

对平台而言仍然是 stdin 任务 → stdout 补丁，对真实 AI 而言不强迫它做做不到的事。

## 三条协议纪律

1. **stdout 的最后一行必须是合法 JSON**，前面允许任意日志——真实 CLI 一定会刷屏。
   同时支持 `--result-file` 兜底，harness 优先读文件（`read_result()`）。
2. **适配器不得自行判定是否解决**。`AgentRunResult` 里没有 `resolved` 字段，
   判定权只属于 Judge。模型开了 `extra="forbid"`，多写一个字段会被当场拒绝。
3. `cost_source=unavailable` 时（订阅制 CLI 常见），平台按 token 用量估算并标成
   `estimated`，报告里必须区分显示。

## 最要紧的一条：任务输入里不能夹带答案

`AgentTaskInput.constraints.protected_paths` 只放**通用规则**（`tests/**`、
`conftest.py` 这类），**绝不能**放该题的 `test_patch_paths`（协议 C-75、C-76）。
把测试补丁实际改动的路径告诉 AI，等于直接指出"官方是改这几个文件来验证的"——
我们连 F2P 的用例 ID 都没下发，不能从这个字段漏出去。

这个模块能做的防守是两层：`extra="forbid"` 让多写的字段直接报错，
`FORBIDDEN_INPUT_KEYS` 再扫一遍适配器私有的 `extra` 字典。
真正的两份清单在 `app.domain.protected_paths`（`agent_visible_patterns()` 是能下发的
那份），组装任务输入的代码在编排层（E5）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import CostSource, IssueLanguage

#: 线上协议的版本号。它描述的是 stdin/stdout 这套报文格式，
#: 和 `app.domain.protocol.PROTOCOL_VERSION`（评测语义协议 v1.2）是两回事，别混。
RUNNER_PROTOCOL_VERSION: Final[Literal["1.0"]] = "1.0"

#: 绝不能出现在下发给 AI 的任务输入里的字段名（协议 C-76、C-44）。
#:
#: 前四个是直接的答案，后几个是定位提示——知道官方改了哪几个文件、
#: 用什么命令跑测试，等于把搜索范围从整个仓库缩到几行。
FORBIDDEN_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "gold_patch",
        "test_patch",
        "test_patch_paths",
        "fail_to_pass",
        "pass_to_pass",
        "test_command",
        "pre_test_command",
        "test_report_path",
        "source_pr_url",
        "enforcement_protected_paths",
    }
)


class ProtocolError(RuntimeError):
    """协议层的错误基类。"""


class ResultParseError(ProtocolError):
    """从适配器的输出里读不出 `AgentRunResult`。

    对应 `InfraOutcome.AGENT_RUNTIME_ERROR`：适配器跑完了但没交出合法结果，
    这是适配器的问题，不是被测 AI 没修好。
    """


class LeakyInputError(ProtocolError):
    """任务输入里夹带了本该瞒着 AI 的东西（协议 C-76）。"""


# ══════════════════════════════════════════════════════════════
# 输入：AgentTaskInput（stdin，一行 JSON）
# ══════════════════════════════════════════════════════════════


class _Strict(BaseModel):
    """协议报文的共同配置：多一个字段就报错。

    宽松解析在这里是有害的。多出来的字段要么是拼错的（本该生效的配置静默失效），
    要么是有人往任务输入里塞了不该塞的东西（泄题）。两种都该当场炸掉。
    """

    model_config = ConfigDict(extra="forbid")


class IssueInput(_Strict):
    """交给被测 AI 的 issue。这是它**唯一**的需求来源。"""

    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    language: IssueLanguage


class RepoInput(_Strict):
    """仓库标识。

    只给名字和 base commit，**不给 URL**：给了 URL，AI 一句 `git clone` 就能
    拉到官方修复（协议 C-44 的同类风险）。工作区里的 git 历史也已经剥离到只剩
    一个提交（E2-T1）。
    """

    name: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class Constraints(_Strict):
    """这次运行的边界。"""

    #: 绝对截止时刻（Unix 毫秒）。用绝对时刻不用"还剩几秒"：适配器可能几秒后才真正
    #: 开始干活，传相对值的话，这段启动时间就被白送给了 AI，两次运行的预算不一样。
    deadline_unix_ms: int = Field(gt=0)
    max_tokens_budget: int | None = Field(default=None, gt=0)
    #: 受保护路径，**只含通用规则**（协议 C-75 的 `agent_visible_protected_paths`）。
    #: 用 `app.domain.protected_paths.agent_visible_patterns()` 生成，
    #: 绝不能用 `enforcement_patterns()`——后者含该题的 `test_patch_paths`（C-76）。
    protected_paths: list[str] = Field(default_factory=list)
    allow_network: bool = True
    #: 允不允许 AI 自己跑测试。允许也**不告诉它测试命令**——
    #: 自己摸索怎么跑测试本身就是 Coding Agent 的能力之一（§9.2）。
    allow_run_tests: bool = True

    def remaining_ms(self, *, now_ms: int | None = None) -> int:
        """离截止还有多少毫秒，已经过期返回 0。"""
        current = time.time() * 1000 if now_ms is None else now_ms
        return max(0, int(self.deadline_unix_ms - current))


class ModelInput(_Strict):
    """用哪个模型、什么温度。温度默认 0，评测要的是可复现。"""

    name: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class AgentTaskInput(_Strict):
    """喂给适配器标准输入的那一行 JSON（§9.2）。

    **这个对象里的每一个字段都会被被测 AI 看到。** 加字段之前先问一句：
    它能不能帮 AI 缩小搜索范围？能的话就不该加。
    """

    protocol_version: Literal["1.0"] = RUNNER_PROTOCOL_VERSION
    task_id: str = Field(min_length=1)
    #: 工作区在**容器里**的路径，不是宿主机路径。
    workspace_path: str = "/workspace"
    issue: IssueInput
    repo: RepoInput
    #: 题目自带的提示（`TaskDefinition.hints_text`）。大部分题没有。
    hints: str | None = None
    constraints: Constraints
    model: ModelInput
    #: 适配器私有配置。会被扫一遍，不许夹带 `FORBIDDEN_INPUT_KEYS` 里的键。
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _no_leaks_in_extra(cls, value: dict[str, Any]) -> dict[str, Any]:
        """扫一遍 `extra`，含嵌套。

        `extra` 是给适配器塞私有配置的口子，也就成了泄题最容易发生的地方——
        它不受字段声明约束，往里面塞一个 `fail_to_pass` 谁都不会发现。
        """
        leaked = sorted(_forbidden_keys_in(value))
        if leaked:
            raise ValueError(f"extra 里夹带了不能下发给 AI 的键：{leaked}（协议 C-76）")
        return value

    def to_stdin_line(self) -> str:
        """序列化成喂给适配器 stdin 的那一行。

        `ensure_ascii=False` 保住中文 issue 的可读性——适配器的日志里会原样回显这行，
        转义成 `\\uXXXX` 之后没法一眼看出任务是什么。
        """
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _forbidden_keys_in(value: Any, *, depth: int = 0) -> set[str]:
    """递归找出 `FORBIDDEN_INPUT_KEYS` 里出现过的键。深度设上限，防自引用结构转不完。"""
    if depth > 8 or not isinstance(value, dict | list):
        return set()
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found |= _forbidden_keys_in(item, depth=depth + 1)
        return found
    found = {key for key in value if key in FORBIDDEN_INPUT_KEYS}
    for item in value.values():
        found |= _forbidden_keys_in(item, depth=depth + 1)
    return found


def assert_no_leak(payload: Mapping[str, Any]) -> None:
    """任务输入落到 stdin 之前的最后一道闸（协议 C-76）。

    `AgentTaskInput` 自己已经拦了两层，这个函数是给"绕过模型直接拼字典"的路径用的，
    比如适配器手工组报文、或者从 JSON 文件读回来重放。检查一次几十微秒。
    """
    leaked = sorted(_forbidden_keys_in(dict(payload)))
    if leaked:
        raise LeakyInputError(f"任务输入里夹带了不能下发给 AI 的键：{leaked}（协议 C-76）")


# ══════════════════════════════════════════════════════════════
# 输出：AgentRunResult（stdout 最后一行）
# ══════════════════════════════════════════════════════════════


class TokenUsage(_Strict):
    """token 用量。拿不到就整个字段传 null，不要填 0 —— 0 和"不知道"是两回事，
    填 0 会让成本统计悄悄偏低。"""

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    #: 不填就按 input + output 补上。各家 CLI 报的 total 含不含缓存读并不统一，
    #: 所以只在缺失时兜底，不去校正适配器报上来的值。
    total: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fill_total(self) -> TokenUsage:
        if self.total == 0 and (self.input or self.output):
            self.total = self.input + self.output
        return self


class AgentError(_Strict):
    """适配器报上来的错误。`code` 给机器看，`message` 给人看。"""

    code: str = Field(min_length=1)
    message: str = ""


class AgentRunResult(_Strict):
    """适配器 stdout 最后一行的那个 JSON（§9.2）。

    **这里没有 `resolved` 字段，也不许加**（协议纪律 2）。判定必须 100% 由测试结果
    推导，让适配器报"我修好了"等于把判定权交给被测方。`extra="forbid"` 会让多写的
    字段直接报错。
    """

    protocol_version: Literal["1.0"] = RUNNER_PROTOCOL_VERSION
    agent_name: str = Field(min_length=1)
    agent_version: str | None = None
    model: str | None = None

    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    #: 适配器进程自己的退出码。不是被测 AI 的结论——非零只说明适配器没跑顺。
    exit_code: int = 0

    #: unified diff。空字符串表示没改动，**不等于** AI 什么都没干
    #: （改的全是受保护路径也会被过滤成空，协议 C-08a）。
    patch: str = ""
    #: 补丁是怎么来的：harness/适配器跑 git diff，还是 AI 自己打印的。
    #: 后者行号容易写错，归因时要能区分开。
    patch_source: Literal["git_diff", "agent_stdout"] = "git_diff"

    token_usage: TokenUsage | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    cost_source: CostSource = CostSource.UNAVAILABLE
    turns: int | None = Field(default=None, ge=0)
    #: 轨迹文件的位置，适配器写文件、harness 来收（§9.5）。
    trajectory_uri: str | None = None
    error: AgentError | None = None

    raw_stdout_bytes: int = Field(default=0, ge=0)
    raw_stderr_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_time_and_cost(self) -> AgentRunResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at 早于 started_at")
        # cost_source 是三选一的枚举，但它和 cost_usd 必须自洽：
        # 报了 reported 却没有金额，报表上会出现"有来源没数字"的空洞；
        # 报了 unavailable 却给了金额，那个数字是哪来的没人说得清
        if self.cost_source is CostSource.UNAVAILABLE and self.cost_usd is not None:
            raise ValueError("cost_source=unavailable 时不能同时给出 cost_usd")
        if self.cost_source is not CostSource.UNAVAILABLE and self.cost_usd is None:
            raise ValueError(f"cost_source={self.cost_source.value} 时必须给出 cost_usd")
        return self

    @property
    def has_patch(self) -> bool:
        """交出了非空补丁。只看有没有内容，不判断补丁对不对。"""
        return bool(self.patch.strip())


# ══════════════════════════════════════════════════════════════
# 解析适配器的输出
# ══════════════════════════════════════════════════════════════

#: 规范的错误码。`AgentError.code` 本身是自由字符串（第三方适配器爱写什么写什么），
#: 但**我们自己写的适配器必须用这几个**——评测单元要靠它把错误翻译成 `infra_outcome`
#: （`app.evaluation.task_run`），而按子串猜（"code 里有没有 timeout"）是不可靠的：
#: Mock 报的是 `deadline_exceeded`，里面根本没有 "timeout" 这个词，
#: 于是超时被错判成了运行时错误（2026-09-05 实测踩到）。
#:
#: 认不出来的错误码一律当成 `AGENT_RUNTIME_ERROR` —— 责任落在被测 AI 这一侧，
#: 不会冤枉平台，代价只是多重试一次。
DEADLINE_EXCEEDED = "deadline_exceeded"
AUTH_FAILED = "auth_failed"
RUNTIME_ERROR = "runtime_error"
#: E3-T4 加的。在此之前没有适配器会起容器，也就不存在"Agent 容器被 OOM 杀掉"这回事。
#: 不能并进 `runtime_error`：按 C-18，OOM 要降配重试，运行时错误不降配，
#: 混在一起的话，一道内存吃紧的题会用同样的配置重试到耗尽预算。
OOM_KILLED = "oom_killed"

#: 适配器可以往 `AgentConfig.artifact_dir` 里写的三个文件名。
#:
#: 这是**适配器和 harness 之间的约定**：适配器按这三个名字写，`execute_task_run()`
#: 按这三个名字捡，捡到什么存什么。定义放在协议里而不是某个适配器里 ——
#: 放在 aider 里的话，编排层就得 import 一个具体适配器才能知道该找什么文件。
AGENT_STDOUT_FILENAME = "stdout.log"
AGENT_STDERR_FILENAME = "stderr.log"
AGENT_TRAJECTORY_FILENAME = "trajectory.jsonl"

#: 报错时截取多长的原文。太短看不出问题，太长会把日志刷爆。
_ERROR_EXCERPT_CHARS = 200


def parse_result_line(line: str) -> AgentRunResult:
    """把一行 JSON 解析成 `AgentRunResult`。"""
    text = line.strip().lstrip("﻿")
    if not text:
        raise ResultParseError("结果行是空的")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResultParseError(
            f"结果行不是合法 JSON（{exc.msg}）：{text[:_ERROR_EXCERPT_CHARS]}"
        ) from exc
    if not isinstance(payload, dict):
        raise ResultParseError(f"结果必须是一个 JSON 对象，收到 {type(payload).__name__}")
    try:
        return AgentRunResult.model_validate(payload)
    except ValueError as exc:
        raise ResultParseError(f"结果字段不合协议：{exc}") from exc


def parse_result_stdout(stdout: str) -> AgentRunResult:
    """从适配器的整段 stdout 里取出结果（协议纪律 1）。

    只认**最后一个非空行**，前面全当日志。真实 CLI 会刷几百行进度和自然语言描述，
    在里面找 JSON 只能靠这条规则。

    两处实际会遇到的脏数据，这里一并处理掉：

    - **尾部空行**：几乎所有 CLI 都会多打一个换行。
    - **回车覆盖**：进度条用 `\\r` 在同一行反复重写，最后一行读出来会是
      `一堆进度\\r{"protocol_version":...}`。只取最后一个 `\\r` 之后的部分。
    """
    for raw in reversed(stdout.splitlines()):
        candidate = raw.rsplit("\r", 1)[-1].strip()
        if candidate:
            return parse_result_line(candidate)
    raise ResultParseError("适配器没有任何输出，读不到结果")


def read_result(stdout: str, *, result_file: Path | None = None) -> AgentRunResult:
    """优先读结果文件，读不到再回到 stdout 最后一行（协议纪律 1 的兜底）。

    为什么要有文件这条路：stdout 是和 CLI 日志共用的通道，一个往 stdout 打日志的
    第三方库、一段没关掉的进度条，都可能把最后一行搅掉。写文件不受这些影响。

    文件存在但内容坏了**不静默回退**到 stdout：那会把"适配器写了个坏文件"
    伪装成"适配器没写文件"，下次同样的问题还是查不出来。
    """
    if result_file is not None and result_file.exists():
        return parse_result_line(result_file.read_text(encoding="utf-8"))
    return parse_result_stdout(stdout)


# ══════════════════════════════════════════════════════════════
# 适配器接口
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """探活结果：CLI 在不在、鉴权行不行、版本号是多少（§9.3）。

    实验开跑前对每个适配器探一次。不探的话，一个过期的 API Key 会让几百次评测
    全部以 `AGENT_AUTH_ERROR` 失败，而这本来在第一秒就能发现。
    """

    ok: bool
    agent_version: str | None = None
    #: 失败原因，或者成功时的补充信息（探到的端点、模型名之类）。
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """适配器的 harness 侧配置——**不下发给 AI** 的那一半。

    模型名、超时、预算这些已经在 `AgentTaskInput` 里了，别在这里重复一份：
    两份配置迟早会不一致，而不一致时以哪份为准没人说得清。这里只放报文里没有、
    也不该让 AI 知道的东西。
    """

    #: 跑这个适配器要用的镜像（`bench-agent:{env}-{agent}`，E2-T3 产出）。
    image: str | None = None
    #: 注入容器的环境变量，**必须已经过 `app.sandbox.container.build_env()` 的白名单**。
    env: Mapping[str, str] = field(default_factory=dict)
    #: 让适配器把结果也写一份到这里（容器内路径），给 `read_result()` 兜底用。
    result_file: Path | None = None
    #: **宿主机**上的一个目录，适配器可以往里写全量 stdout、stderr 和轨迹
    #: （文件名见各适配器的常量），跑完由 `execute_task_run()` 捡走存成制品。
    #:
    #: 为什么不写工作区：工作区里多出来的文件会进 `git diff`，变成补丁里的一处改动。
    #: 为什么不由适配器自己存制品：`app.runner` 看不见编排层，而且制品存哪
    #: 是 harness 的事，适配器不该知道。
    artifact_dir: Path | None = None
    #: 追加给底层 CLI 的参数。
    extra_args: tuple[str, ...] = ()


@runtime_checkable
class AgentRunner(Protocol):
    """一个被测 AI 的适配器（§9.3）。

    实现类：`MockRunner` / `OracleRunner` / `NoopRunner`（E3-T2，在
    `app.runner.adapters` 里）、`AiderRunner` / `ClaudeCodeRunner` /
    `MiniAgentRunner`（Week 2–3）。

    每个实现都必须跑通契约测试套件（`tests/contract/runner_contract.py` 的
    `AgentRunnerContract`），一共六条。
    """

    #: 适配器名字，会写进结果和排行榜。
    name: str

    def probe(self) -> ProbeResult:
        """检查这个适配器现在能不能用。不该有副作用，也不该花钱。"""
        ...

    def run(self, task: AgentTaskInput, workspace: Any, config: AgentConfig) -> AgentRunResult:
        """跑一道题。

        `workspace` 是 `app.sandbox.workspace.Workspace`——这里标成 `Any` 是为了让
        协议本身不依赖沙箱，写假适配器时不用先物化一个真工作区。真实适配器按
        `Workspace` 用它：`workspace.path` 挂进容器，`workspace.base_sha` 拿去
        `git diff`（Agent 干完活可能自己 commit 过，裸 `git diff` 是空的）。

        **超时不靠这个方法自觉**：墙钟到点由 harness 用 `docker stop` 强杀
        （§9.6）。这里的 `deadline_unix_ms` 是给 AI 自己收尾用的软预算。
        """
        ...


@runtime_checkable
class ImageBuildingRunner(AgentRunner, Protocol):
    """需要自带镜像层的适配器（§9.3 里 `AgentRunner` 的第四个方法）。

    拆成单独一个 Protocol，是因为 Mock / Oracle / Noop 根本不需要镜像——
    让它们实现一个只会 `raise NotImplementedError` 的方法，是契约测试永远测不到的
    死代码。真实 CLI 适配器（Aider 要 pip 装、Claude Code 要 npm 装）才实现它，
    镜像分层本身是 E2-T3 的事。
    """

    def build_image(self, environment_id: str, base_image: str) -> str:
        """在环境镜像上加一层这个 Agent 的 CLI，返回新镜像的 tag。"""
        ...


__all__ = [
    "FORBIDDEN_INPUT_KEYS",
    "RUNNER_PROTOCOL_VERSION",
    "AgentConfig",
    "AgentError",
    "AgentRunResult",
    "AgentRunner",
    "AgentTaskInput",
    "Constraints",
    "ImageBuildingRunner",
    "IssueInput",
    "LeakyInputError",
    "ModelInput",
    "ProbeResult",
    "ProtocolError",
    "RepoInput",
    "ResultParseError",
    "TokenUsage",
    "assert_no_leak",
    "parse_result_line",
    "parse_result_stdout",
    "read_result",
]
