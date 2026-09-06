"""Aider 适配器：第一个真实的被测 AI（E3-T4）。

## 它和前面三个哨兵有什么不同

Oracle / Noop / Mock 都**不碰工作区**，直接在结果里交一段补丁字符串。Aider 反过来：
它在容器里把 `/workspace` 下的文件改掉，补丁是我们跑 `git diff` 生成的
（`04-runner-protocol.md` §9.1 里的 workspace-mutation 模式）。

这也意味着这是第一个真的会**起容器**的适配器。之前 `runner.run()` 抛出的任何异常
都会被 `execute_task_run()` 记成 `AGENT_RUNTIME_ERROR`（记在被测 AI 头上），
而"docker 连不上""镜像不在"显然不是 AI 的错 —— 那条路径这次一并补了。

## 为什么容器里没有 wrapper 脚本

一开始想的是往镜像里放一个脚本：读 stdin 的任务 JSON，调 aider，最后打印结果 JSON。
不这么做，两个理由：

1. `run_in_container()` 不支持 stdin（它走 docker SDK 的 create + start，
   要接 stdin 得挂 socket）。为了 Agent 阶段的便利去动沙箱层不划算 ——
   那一层还被测试阶段共用。
2. §9.1 已经把协议边界定在 **Adapter** 上，不是 Agent 进程上。这个类就是那个
   Adapter，字面的 stdin/stdout 形式由 `cli/runner.py` 提供。

所以容器命令直接就是 `aider ... --message "<提示>"`，stdout 由沙箱层收回来，
**token 和 cost 的解析全在宿主机这边**。好处很实在：容易出错的解析逻辑都是纯函数，
普通单测就能覆盖，不需要 Docker、不需要 API Key，改规则也不用重建镜像。

## 几个参数为什么必须给

| 参数 | 不给会怎样 |
|:---|:---|
| `--yes-always` | 它会停下来等人按回车，容器一直挂到超时 |
| `--no-auto-commits` | 它会自己 `git commit`，裸 `git diff` 就成了空的 |
| `--no-gitignore` | 它会往仓库的 `.gitignore` 里加 `.aider*`，那会变成补丁里的一处改动 |
| `--no-pretty` | 彩色转义和光标控制符会混进 stdout，用量行就解析不出来了 |
| `--no-check-update` | 每次启动联网查新版本，慢且没意义 |

补丁交的是**原始 diff**，受保护路径的改动留在里面不动 —— 过滤是平台在 E3-T3
做的事（协议 C-08b，契约第 4 条）。适配器自己过滤掉的话，
"AI 试图改测试文件"这条证据就没了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import CostSource, IssueLanguage
from app.infrastructure.logging import get_logger
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
    get_docker_client,
)
from app.sandbox.container import run_in_container as _run_in_container

logger = get_logger(__name__)

#: 装了 aider 的镜像。和 `images/aider/Dockerfile`、Makefile 的 `AIDER_IMAGE` 对齐。
#: 只是默认值 —— 真实评测由 `agent_configs.params["image"]` 决定。
DEFAULT_AIDER_IMAGE = "bench-agent:py311-aider"

#: 镜像里钉死的 aider 版本。只在 stdout 里读不到 banner 时兜底，
#: 读得到就以 stdout 为准（镜像和这个常量可能不同步，stdout 是现场事实）。
PINNED_AIDER_VERSION = "0.86.2"

#: 容器超时后留给 aider 落盘的宽限期（秒）。比默认的 10 秒长：
#: 协议 C-09a 要求超时也要保存补丁，而 aider 收到 SIGTERM 后要把改了一半的文件写完。
AIDER_STOP_GRACE_S = 20

#: `run()` 至少要有这么多秒才值得起容器。低于它直接按截止已过返回 ——
#: 起一个容器、拉起 Python、装载 aider 本身就要十几秒，
#: 剩几秒钟的话，唯一确定的结果是白花一次容器启动的时间。
MIN_USEFUL_SECONDS = 30

#: `AgentError.message` 里最多放多少字符（两条流平分）。太短看不出问题，
#: 太长会把 `evaluation_task_runs.error_message_excerpt` 撑爆。
ERROR_EXCERPT_CHARS = 2000


#: aider 的用量行。2026-09-05 实测抓到的两种真实形态：
#:
#:     Tokens: 5.3k sent, 412 received. Cost: $0.0012 message, $0.0034 session.
#:     Tokens: 3.2k sent, 3.1k cache hit, 403 received. Cost: $0.00061 message, $0.0011 session.
#:
#: 中间那个 `cache hit` 是提示缓存命中的 token 数，**只在命中时才打**。
#: 第一次冒烟是冷启动、没命中，格式恰好和我编的样本一样；正式跑四道题时
#: 每次都命中，于是一条都解析不出来，token 和 cost 全成了空值 ——
#: 而这不会报错，只会让成本统计安静地变成一片空白。
#:
#: 三处可选各有理由：
#: - `cache hit`：不是每次都有
#: - 整个 Cost 段：litellm 不认识的模型没有价目表，那时只打 token 不打钱
#: - `session` 那半截：日志被截断时可能只剩前半句
#:
#: 拿不到就报 unavailable，**绝不填 0**（协议纪律 3、契约第 5 条）。
USAGE_RE = re.compile(
    r"Tokens:\s*(?P<sent>[\d.,]+\s*[kKmM]?)\s*sent"
    r"(?:\s*,\s*(?P<cache>[\d.,]+\s*[kKmM]?)\s*cache\s*hit)?"
    r"\s*,\s*(?P<received>[\d.,]+\s*[kKmM]?)\s*received\."
    r"(?:\s*Cost:\s*\$(?P<message_cost>[\d.]+)\s*message"
    r"(?:\s*,\s*\$(?P<session_cost>[\d.]+)\s*session)?)?"
)

#: 启动横幅，例如 `Aider v0.86.2`。版本以现场为准，不以常量为准。
VERSION_RE = re.compile(r"^\s*Aider\s+v(?P<version>[0-9][^\s,]*)", re.MULTILINE)

#: 编辑落地的那一行，例如 `Applied edit to auth/password.py`。轨迹靠它还原改了哪些文件。
APPLIED_EDIT_RE = re.compile(r"^Applied edit to (?P<path>.+?)\s*$", re.MULTILINE)

#: 判成鉴权失败的标记。**写成挤掉空白之后的样子**，理由见 `squash()`。
#:
#: 这里只能靠子串：对面是外部 CLI 打出来的自由文本，没有一张可查的表。
#: 所以把清单集中放在这一处，不要散到代码里 —— 散开之后，加一种新的鉴权报错
#: 要改几个地方，漏一个就是几百次评测被记成"AI 自己崩了"。
#:
#: 判错的代价不对称：鉴权失败按 C-18 重试 3 次、运行时错误重试 1 次。
#: 把鉴权当成运行时错误，一个配错的 Key 会安静地把解决率拉到 0。
AUTH_MARKERS: tuple[str, ...] = (
    "authenticationerror",
    "authentication_error",
    "authenticationfails",
    "invalid_api_key",
    "incorrectapikey",
    "invalidapikey",
    "noapikey",
    "apikeynotfound",
    "unauthorized",
)

#: HTTP 401。单独用正则而不是塞进上面的清单：裸写 `"401"` 会被
#: `Tokens: 1401 sent` 命中，于是一次正常的运行被判成鉴权失败。
AUTH_STATUS_RE = re.compile(r"\b401\b")

#: litellm 抛出来的异常，例如 `litellm.BadRequestError`、`litellm.RateLimitError`。
#:
#: **2026-09-05 实测：模型侧失败时 aider 打完这行照样退 0。** 只看退出码的话，
#: 一个配错的 Key 会被记成"AI 跑完了但什么都没改"（UNRESOLVED），
#: 解决率安静地掉到 0，而排行榜上看不出任何异常 —— 这正是它必须在这里被认出来的原因。
LITELLM_ERROR_RE = re.compile(r"litellm\.\w*(?:error|exception)")


@dataclass(frozen=True, slots=True)
class AiderUsage:
    """从 aider 的 stdout 里读出来的用量。

    `cost_usd` 为 None 表示**读不出来**，不是 0 —— 两者在报表里的待遇完全不同
    （协议纪律 3）。
    """

    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    #: 用量行出现了几次。aider 每轮和模型交互打一行，正好可以当 `turns`。
    turns: int
    #: 提示缓存命中的 token 数。**是 `input_tokens` 的一部分，不是另加的**——
    #: 加进总数会让 token 统计凭空多出一截。DeepSeek 的缓存命中便宜一个数量级，
    #: 单独记下来，成本分析时才解释得了"为什么 token 差不多但钱差很多"。
    cache_read_tokens: int = 0


def parse_token_count(raw: str) -> int:
    """把 `5.3k` / `1,024` / `2.1M` 这种写法变成整数。

    aider 为了好看会把大数写成 `5.3k`，直接 `int()` 会抛异常。
    """
    text = raw.strip().replace(",", "")
    factor = 1
    if text and text[-1] in "kK":
        factor, text = 1_000, text[:-1]
    elif text and text[-1] in "mM":
        factor, text = 1_000_000, text[:-1]
    return round(float(text) * factor)


def parse_usage(stdout: str) -> AiderUsage | None:
    """从 stdout 里汇总 token 和 cost。一行都没匹配上就返回 None。

    **token 累加，cost 取最后一行的 session 值。** aider 每轮打一行，
    `message` 是这一轮的钱、`session` 是从开跑到现在的累计 —— 把 session 也加起来
    会得到一个成倍偏大的数字。
    """
    matches = list(USAGE_RE.finditer(unwrap(stdout)))
    if not matches:
        return None

    def total(group: str) -> int:
        return sum(parse_token_count(m.group(group)) for m in matches if m.group(group))

    session_costs = [m.group("session_cost") for m in matches if m.group("session_cost")]
    return AiderUsage(
        input_tokens=total("sent"),
        output_tokens=total("received"),
        cost_usd=float(session_costs[-1]) if session_costs else None,
        turns=len(matches),
        cache_read_tokens=total("cache"),
    )


def parse_version(stdout: str) -> str | None:
    """从启动横幅里读 aider 的版本。读不到返回 None。"""
    match = VERSION_RE.search(stdout)
    return match.group("version") if match else None


def unwrap(text: str) -> str:
    """把折行接回去：连续空白（含换行）压成**一个空格**。

    aider 按终端宽度折行，一条用量行经常被劈成两半。2026-09-05 实测抓到的三种劈法：

        Cost: $0.00050 message, $0.00050\nsession.
        Cost: $0.0012 message, $0.0012 \nsession.
        Cost: $0.00074 message, \n$0.00074 session.

    劈在哪儿看消息本身有多长，没有规律。压成一个空格之后这三种都一样了。

    和 `squash()` 的区别：那个压成**空**，用来做子串匹配，顺带把被劈开的单词接回去；
    这个压成**一个空格**，用来做正则匹配，词与词的边界必须留着。
    """
    return re.sub(r"\s+", " ", text)


def squash(text: str) -> str:
    """挤掉全部空白并转小写，再拿去比对。

    aider 按终端宽度硬折行，而且**会从单词中间折**。2026-09-05 实测抓到的原文：

        litellm.BadRequestError: DeepseekException - {"error":{"message":"Authentication
        Fails, Your api key: ****9ca8 is
        invalid","type":"authentication_error","param":null,"code":"invalid_request_erro
        r"}}

    `Authentication Fails` 被折成了两行，`invalid_request_error` 被从
    `erro | r` 中间劈开。照原样做子串匹配的话，一段报错认不认得出来取决于它
    恰好折在哪个字符上 —— 这种 bug 只在某些消息长度下出现，最难复现。

    挤掉空白之后这两个问题一起没了，代价是清单里的标记也要写成没有空格的形式。
    """
    return re.sub(r"\s+", "", text).lower()


def looks_like_auth_failure(text: str) -> bool:
    """这段输出像不像鉴权失败。清单见 `AUTH_MARKERS`。"""
    squashed = squash(text)
    return any(marker in squashed for marker in AUTH_MARKERS) or bool(
        AUTH_STATUS_RE.search(squashed)
    )


def has_model_side_failure(text: str) -> bool:
    """输出里有没有 litellm 抛出来的异常。

    单独一个函数是因为它回答的问题和退出码无关：aider 打完这种报错**照样退 0**。
    """
    return bool(LITELLM_ERROR_RE.search(squash(text)))


def build_trajectory(stdout: str, *, started_at: datetime) -> str:
    """把 stdout 转成 §9.5 的 JSONL 轨迹。每行一个事件。

    只提取两类**确定能对上**的事件：每轮的 token 用量，和每次落地的文件编辑。
    不去猜自然语言里哪句是"思考"哪句是"结论" —— 猜出来的轨迹会被当成证据用在
    失败归因上，猜错比没有更糟。全量 stdout 另存一份（§9.5 最后一句）。
    """
    ts = int(started_at.timestamp() * 1000)
    events: list[dict[str, Any]] = []
    for match in USAGE_RE.finditer(unwrap(stdout)):
        events.append(
            {
                "ts": ts,
                "type": "llm_usage",
                "input": parse_token_count(match.group("sent")),
                "output": parse_token_count(match.group("received")),
            }
        )
    for match in APPLIED_EDIT_RE.finditer(stdout):
        events.append(
            {
                "ts": ts,
                "type": "tool_call",
                "name": "edit_file",
                "summary": match.group("path"),
            }
        )
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


#: 提示词的骨架。issue 用什么语言就用哪一份 —— 中文题干配英文指令，
#: 模型有时会跟着指令切回英文回答，那会让轨迹和日志变得难读。
_PROMPT_TEMPLATES: dict[IssueLanguage, str] = {
    IssueLanguage.ZH: (
        "请修复下面这个缺陷。\n\n"
        "## {title}\n\n{body}\n\n"
        "---\n\n"
        "几点要求：\n\n"
        "1. 只改产品代码。测试文件（{protected}）就算改了也会被丢弃，别在上面花时间。\n"
        "2. 不要新增第三方依赖，跑测试的容器是断网的，装不上。\n"
        "3. 改完就结束，不用写解释，也不用写总结。\n"
    ),
    IssueLanguage.EN: (
        "Please fix the bug described below.\n\n"
        "## {title}\n\n{body}\n\n"
        "---\n\n"
        "Requirements:\n\n"
        "1. Only change production code. Edits to test files ({protected}) are discarded, "
        "so do not spend effort there.\n"
        "2. Do not add third-party dependencies; the test container has no network.\n"
        "3. Stop when the fix is in place. No explanation or summary is needed.\n"
    ),
}


def build_message(task: AgentTaskInput) -> str:
    """拼给 aider 的 `--message`。

    `protected_paths` 直接来自 `task.constraints`，那是
    `agent_visible_patterns()` 的产物（通用规则），**不含**该题的
    `test_patch_paths` —— 后者下发出去等于告诉 AI 官方改了哪几个文件（协议 C-76）。
    这里原样用，不要自己另拼一份。
    """
    template = _PROMPT_TEMPLATES.get(task.issue.language, _PROMPT_TEMPLATES[IssueLanguage.EN])
    return template.format(
        title=task.issue.title,
        body=task.issue.body,
        protected=", ".join(task.constraints.protected_paths) or "tests/**",
    )


def build_command(
    task: AgentTaskInput, model: str, *, extra_args: tuple[str, ...] = ()
) -> list[str]:
    """拼容器里要跑的那条命令。

    每个开关为什么必须给，见模块开头那张表。用列表不用字符串：题干里带引号、
    反引号、`$` 的情况多得是，走 shell 会改变命令的含义。
    """
    return [
        "aider",
        "--model",
        model,
        "--yes-always",
        "--no-auto-commits",
        "--no-gitignore",
        "--no-pretty",
        "--no-stream",
        "--no-check-update",
        "--no-detect-urls",
        "--no-show-model-warnings",
        *extra_args,
        "--message",
        build_message(task),
    ]


class AiderRunner:
    """在容器里跑 aider，跑完从工作区抓补丁。

    构造参数都是可选的，真实评测下一个都不用传 —— 镜像来自 `AgentConfig.image`，
    模型来自 `AgentTaskInput.model.name`（也就是 `agent_configs.model_name`）。

    `model` 这个构造参数只有两个用处：契约测试（套件里那份合成任务带的是
    `contract-fake-model`，真发给 aider 会直接报错）和命令行冒烟。**生产不要传** ——
    传了就等于绕过数据库里那份配置，报表上写的模型和实际用的会对不上。
    """

    name = "aider"

    def __init__(
        self,
        *,
        image: str | None = None,
        model: str | None = None,
        run_container: Any = None,
    ) -> None:
        self._image = image
        self._model = model
        #: 测试用的接缝：传一个假的进来，不起容器也能验命令拼装和故障映射。
        self._run_container = run_container or _run_in_container

    # ── 探活 ────────────────────────────────────────────────

    def probe(self) -> ProbeResult:
        """看镜像在不在。**不起容器、不调模型、不花钱。**

        探不到 Key 是有意的：Key 是 `AgentConfig.env` 的内容，那是每次运行才拼出来的，
        探活拿不到。真正能在第一秒挡住过期 Key 的是 E5 编排层的开跑前检查，
        这里假装能探反而会给出虚假的安心。
        """
        image = self._image or DEFAULT_AIDER_IMAGE
        try:
            get_docker_client().images.get(image)
        except Exception as exc:
            return ProbeResult(ok=False, detail=f"镜像 {image} 不可用：{type(exc).__name__}: {exc}")
        return ProbeResult(
            ok=True,
            agent_version=PINNED_AIDER_VERSION,
            detail=f"镜像 {image} 就绪；模型由每次运行的任务输入决定",
        )

    # ── 干活 ────────────────────────────────────────────────

    def run(self, task: AgentTaskInput, workspace: Any, config: AgentConfig) -> AgentRunResult:
        """跑一道题：起容器改工作区 → `git diff` 抓补丁 → 解析 token/cost。"""
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
        self._dump_side_files(config, container, started_at)

        # 补丁在容器结束之后才抓，超时被杀也照抓 —— 协议 C-09a：超时也要保存补丁。
        # 抓的是**原始** diff，受保护路径的改动留着，过滤是平台的事（C-08b）
        patch = capture_workspace_diff(workspace)
        usage = parse_usage(container.stdout)

        return self._result(
            started_at=started_at,
            model=self._model_for(task),
            version=parse_version(container.stdout) or PINNED_AIDER_VERSION,
            exit_code=container.exit_code,
            patch=patch,
            usage=usage,
            error=_error_for(container),
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
            image=config.image or self._image or DEFAULT_AIDER_IMAGE,
            command=build_command(task, self._model_for(task), extra_args=config.extra_args),
            timeout_s=timeout_s,
            stage=Stage.AGENT,
            # 被测 AI 要连大模型 API，所以 Agent 阶段是联网的。测试阶段永远断网（C-31），
            # 那由测试执行器自己保证，两边互不影响
            network=NetworkMode.BRIDGE if task.constraints.allow_network else NetworkMode.NONE,
            mounts=(BindMount.workspace(Path(workspace.path)),),
            workdir=WORKSPACE_TARGET,
            env=config.env,
            stop_grace_s=AIDER_STOP_GRACE_S,
            run_id=task.task_id,
        )

    def _dump_side_files(
        self, config: AgentConfig, container: ContainerResult, started_at: datetime
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
            AGENT_TRAJECTORY_FILENAME: build_trajectory(container.stdout, started_at=started_at),
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
        version: str = PINNED_AIDER_VERSION,
        patch: str = "",
        usage: AiderUsage | None = None,
        error: AgentError | None = None,
        raw_stdout_bytes: int = 0,
        raw_stderr_bytes: int = 0,
        trajectory_uri: str | None = None,
    ) -> AgentRunResult:
        """按同一套规则组装结果。

        成本那三行是这里最要紧的部分：**读不出来就报 `unavailable` 并把
        `cost_usd` 留成 None**，不许拿 0 顶替。填 0 会让成本统计悄悄偏低，
        而且从数据上看不出是"真的没花钱"还是"没读出来"（协议纪律 3，契约第 5 条）。
        """
        finished_at = datetime.now(UTC)
        token_usage = None
        if usage is not None:
            token_usage = TokenUsage(
                input=usage.input_tokens,
                output=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
                # 缓存命中是 input 的一部分，不能再加一遍
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


def _error_for(container: ContainerResult) -> AgentError | None:
    """容器怎么结束的 → 适配器自报的错误码（`_AGENT_ERROR_TO_INFRA` 会查这个码）。

    **顺序不能换**：先看 OOM 再看超时。两种情况的退出码都是 137，反过来判会把
    内存超限当成超时 —— 前者按 C-18 要降配重试，后者直接判 AI 没修好（协议 C-19b）。
    这和 `classify_outcome()` 是同一条纪律。

    **退出码 0 不等于跑成功。** 2026-09-05 第一次真跑就撞上了：DeepSeek 的 Key 无效，
    aider 把 `litellm.BadRequestError` 打在 stdout 上，然后**正常退出，退出码 0**。
    只看退出码的话，这一次会被记成"AI 跑完了但什么都没改"，判成 UNRESOLVED ——
    一个配错的 Key 就这样安静地把解决率拉到 0，而且排行榜上看不出任何异常。
    所以这里要另外查一遍模型侧的报错。

    反过来，退出码 0 且没有模型侧报错时，空补丁**就是**正常结果："AI 没改出东西"
    是它自己的问题，对应 UNRESOLVED，不是平台故障。在这里报错的话，一次正常的
    "没修好"会触发重试，白花钱还把归因引向错误的方向。
    """
    if container.oom_killed:
        return AgentError(code=OOM_KILLED, message="Agent 容器内存超限被杀")
    if container.timed_out:
        return AgentError(code=DEADLINE_EXCEEDED, message="Agent 容器墙钟超时被杀")

    text = container.stdout + "\n" + container.stderr
    if container.exit_code == 0 and not has_model_side_failure(text):
        return None
    excerpt = failure_excerpt(container)
    if looks_like_auth_failure(text):
        return AgentError(code=AUTH_FAILED, message=f"疑似鉴权失败：{excerpt}")
    return AgentError(
        code=RUNTIME_ERROR, message=f"aider 失败（退出码 {container.exit_code}）：{excerpt}"
    )


def failure_excerpt(container: ContainerResult) -> str:
    """给人看的报错摘要，两条流各截一段尾巴。

    **不能只取 stderr。** aider 把模型侧的报错打在 **stdout** 上，stderr 里往往
    只有一句 `Warning: Input is not a terminal (fd=0).` —— 只取 stderr 的话，
    `evaluation_task_runs.error_message_excerpt` 那一列里就只剩这句废话，
    而真正的原因在 stdout 里躺着（2026-09-05 实测踩到）。

    两条各截一半而不是拼起来再截：拼完再截的话，stderr 一长，stdout 的尾巴
    （报错就在那儿）会被挤掉。
    """
    half = ERROR_EXCERPT_CHARS // 2
    parts = [
        f"{label}: {stream.strip()[-half:]}"
        for label, stream in (("stdout", container.stdout), ("stderr", container.stderr))
        if stream.strip()
    ]
    return "\n".join(parts)


__all__ = [
    "AUTH_MARKERS",
    "DEFAULT_AIDER_IMAGE",
    "PINNED_AIDER_VERSION",
    "AiderRunner",
    "AiderUsage",
    "build_command",
    "build_message",
    "build_trajectory",
    "failure_excerpt",
    "has_model_side_failure",
    "looks_like_auth_failure",
    "parse_token_count",
    "parse_usage",
    "parse_version",
    "squash",
    "unwrap",
]
