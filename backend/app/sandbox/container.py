"""容器执行器（E2-T2）。

被测 AI 干活、以及事后跑测试，都在 Docker 容器里进行。**起容器只有这一个入口**
（`run_in_container`），四件事在这里一次性做对：

1. **资源限额** —— CPU、内存、进程数三条硬限，一道坏题不能把整台机器拖垮。
2. **墙钟超时** —— 到点先 `SIGTERM`，宽限期过了再 `SIGKILL`。
3. **网络策略** —— 测试阶段一律断网（协议 C-31）。
4. **善后** —— 不管怎么退出容器都要删掉；Worker 启动时还要回收上次崩溃留下的孤儿。

## 三个必须记住的坑

**坑一：OOM 和超时的退出码都是 137。** 内存超限是平台的问题（该重试），执行超时是
被测 AI 的问题（该判它没修好），两者语义相反却共用一个退出码。唯一可靠的判据是
`docker inspect` 里的 `.State.OOMKilled`（协议 C-06、C-07）。`classify_outcome()`
里先看 OOM 再看超时，顺序不能换。

**坑二：不能开 `auto_remove`。** 它会在容器一退出就删掉容器，而我们必须在退出**之后**
读 `.State.OOMKilled` —— 容器没了就读不到，OOM 会被静默当成普通失败。所以这里手工
在 `finally` 里删。

**坑三：容器不继承宿主机环境，但也别指望它自动干净。** docker 确实不传宿主机的环境
变量，可是 `docker run` 命令行会把 `~/.docker/config.json` 里的 `proxies` 塞进每个容器
（这台机器上实测如此）。我们走 Python SDK，SDK 不读那份配置，所以容器里只有
`build_env()` 拼出来的那些 —— 这是有意的，别改成用命令行起容器。

## 不在这一步做的事

- **补丁怎么打、测试怎么跑**：那是 E4-T2 的事，这里只负责"把一条命令关进笼子里跑"。
- **镜像从哪来**：`create()` 不会自动拉镜像，镜像不存在直接抛 `ImageNotFoundError`。
  评测跑到一半去拉镜像会打爆时间预算，也让结果不可复现（ADR-008），预建镜像是 E2-T3。
- **出网白名单**：`NetworkMode.BRIDGE` 现在就是 docker 默认桥接，能连整个互联网。
  只放行 LLM API 的代理网络是 E2-T4。
"""

from __future__ import annotations

import os
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import DockerException, ImageNotFound, NotFound

from app.domain.enums import InfraOutcome
from app.infrastructure.logging import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

#: 容器上打的标记。孤儿回收靠它认领"这是我们起的容器"。
#: 不用名字前缀：名字是我们自己编的字符串，标签是 daemon 侧的结构化字段，
#: `docker ps --filter label=...` 能直接筛，不会误伤别的项目同名的容器。
BENCH_LABEL = "bench.owner"
BENCH_LABEL_VALUE = "coding-agent-benchmark"
#: 这个容器属于哪一步、哪一次评测。排查时靠它把容器和 task_run 对上。
BENCH_STAGE_LABEL = "bench.stage"
BENCH_RUN_LABEL = "bench.run_id"

#: 工作区在容器里的挂载点。宿主机路径每次评测都不一样，容器里这个路径是固定的，
#: 这样题目里的 `test_command` 不用关心宿主机的目录结构。
WORKSPACE_TARGET = "/workspace"

#: 收一条流的日志上限（字节）。跑飞的 Agent 能刷出几个 GB，全读进内存会把 Worker 撑爆。
#: 超过就截断并在结果里标出来 —— 完整日志由调用方直接落制品（E0-T4），不经过这里。
MAX_LOG_BYTES = 4 * 1024 * 1024

#: `docker stop` 之后再等多久去取退出码。宽限期本身由 `ContainerSpec.stop_grace_s` 定，
#: 这里加的是"daemon 处理这条请求"的余量。
STOP_WAIT_SLACK_S = 15

#: docker API 调用的默认超时（秒）。等容器结束那一下会用 `ContainerSpec.timeout_s` 覆盖。
DOCKER_API_TIMEOUT_S = 60

#: harness 自己以 root 跑时（比如 Worker 在容器里），容器退到这个用户。
#: 65534 是 Linux 约定的 nobody。
NOBODY_UID = 65534
NOBODY_GID = 65534

#: 固定成 1980-01-01。不用 0：zip 和 wheel 的时间戳字段存不下 1980 之前的日期，
#: 有些打包工具会直接报错。
SOURCE_DATE_EPOCH = "315532800"

#: 容器里固定注入的环境变量（协议 C-37，`05-sandbox.md` §10.3「确定性」一行）。
#: 少了它们，同一个补丁两次跑可能得到不同的测试结果 —— 字典序、时区、哈希种子都会变。
DETERMINISM_ENV: dict[str, str] = {
    "TZ": "UTC",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
}

#: 允许注入 Agent 容器的环境变量名（§10.3「环境变量」一行）。名单之外一律不传。
#:
#: 名字不能改成带前缀的形式：被测 CLI（aider、claude-code）自己就认这些名字，
#: 改名等于要在每个适配器里再做一次映射。
AGENT_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # 各家大模型的密钥
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        # 中转网关要用
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "DEEPSEEK_BASE_URL",
        # 出网走白名单代理（E2-T4）。大小写两套都留：curl 只认小写，
        # requests / httpx 两套都认，少哪一套都会有工具绕过代理直连
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


# ══════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════


class SandboxError(RuntimeError):
    """沙箱自己出的问题。对应 `InfraOutcome.SANDBOX_ERROR`，是平台故障不是 AI 的锅。"""


class DockerUnavailableError(SandboxError):
    """连不上 docker daemon。"""


class ImageNotFoundError(SandboxError):
    """镜像不在本地。这里**不会**自动去拉，理由见模块文档。"""

    def __init__(self, image: str) -> None:
        self.image = image
        super().__init__(
            f"镜像 {image} 不在本地。先用 `bench images build` 预建（E2-T3），评测过程中不拉镜像"
        )


class EnvNotAllowedError(SandboxError):
    """想注入一个不在白名单里的环境变量。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"环境变量 {name} 不在白名单里，不会注入容器。"
            f"确实需要就加进 AGENT_ENV_ALLOWLIST，并想清楚它会不会把答案带进容器"
        )


# ══════════════════════════════════════════════════════════════
# 规格
# ══════════════════════════════════════════════════════════════


class NetworkMode(StrEnum):
    """容器的网络。

    NONE 对应 `--network none`。协议 C-31 规定测试阶段必须断网：联网的测试可能去
    PyPI 装包（结果就不可复现了），被测代码也可能直接从网上取到正确答案。

    BRIDGE 是 docker 默认桥接，能连整个互联网，暂时给 Agent 阶段用。
    E2-T4 会加一个只放行 LLM API 的模式，那时 Agent 阶段改用它。
    """

    NONE = "none"
    BRIDGE = "bridge"


class Stage(StrEnum):
    """这个容器在跑评测的哪一步。

    只影响一件事：超时该记成 `AGENT_TIMEOUT` 还是 `TEST_TIMEOUT`。这两个值在协议
    C-18 的映射表里待遇完全不同 —— 前者判 AI 没修好且不重试，后者要走 C-19b 的
    对照组流程。
    """

    AGENT = "AGENT"
    TEST = "TEST"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """一个容器的资源上限。

    三个数直接来自题目的 `sandbox_cpu` / `sandbox_memory_mb` / `sandbox_pids_limit`
    （`03-benchmark-spec.md` §7.1），默认值和题目 schema 里写的一致。

    为什么不直接收一个 `TaskDefinition`：模块边界不允许 sandbox 依赖 benchmark
    （pyproject 里的 import-linter 契约），映射由上层调用方做。
    """

    #: CPU 核数，可以是小数。1.0 = 一个核跑满。
    cpus: float = 1.0
    #: 内存上限（MiB）。swap 会被设成同一个值，等于禁用 swap ——
    #: 留了 swap 的话，内存超限的题不会被 OOM 杀掉，而是慢到超时，故障类型就判错了。
    memory_mb: int = 1536
    #: 容器内最多多少个进程/线程。挡的是 fork 炸弹。
    pids_limit: int = 512
    #: `/tmp` 的 tmpfs 大小（MiB）。tmpfs 占的是内存，也算在 `memory_mb` 里面。
    tmpfs_mb: int = 512

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError(f"cpus 要大于 0，收到 {self.cpus}")
        # docker 自己的下限就是 6 MiB，再小 daemon 直接拒绝
        if self.memory_mb < 6:
            raise ValueError(f"memory_mb 至少 6，收到 {self.memory_mb}")
        if self.pids_limit < 1:
            raise ValueError(f"pids_limit 至少 1，收到 {self.pids_limit}")
        if self.tmpfs_mb < 1:
            raise ValueError(f"tmpfs_mb 至少 1，收到 {self.tmpfs_mb}")

    @property
    def nano_cpus(self) -> int:
        """docker API 收的是纳核数，`--cpus=1.5` 等于 1_500_000_000。"""
        return int(self.cpus * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class BindMount:
    """把宿主机的一个目录挂进容器。"""

    source: Path
    target: str
    read_only: bool = False

    def __post_init__(self) -> None:
        # 相对路径会被 docker 当成"卷名"而不是路径，于是它默默建一个空卷挂进去，
        # 容器里看到的是空目录，不报错。这个坑很难查，在这里挡掉
        if not self.source.is_absolute():
            raise ValueError(f"挂载源必须是绝对路径，收到 {self.source}")
        if not self.target.startswith("/"):
            raise ValueError(f"挂载点必须是绝对路径，收到 {self.target}")

    @classmethod
    def workspace(cls, source: Path, *, read_only: bool = False) -> BindMount:
        """把物化好的工作区挂到容器的 `/workspace`。"""
        return cls(source=source, target=WORKSPACE_TARGET, read_only=read_only)


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """起一个容器要的全部信息。"""

    #: 镜像标签或 digest。不存在就抛 `ImageNotFoundError`，不会自动去拉。
    image: str
    #: 要跑的命令。用列表不用字符串：字符串形式会经过 shell，
    #: 文件名里一个空格或者引号就能改变命令的含义。
    command: Sequence[str]
    #: 墙钟超时（秒）。来自题目的 `agent_timeout_s` / `test_timeout_s`。
    timeout_s: int
    stage: Stage = Stage.TEST
    limits: ResourceLimits = ResourceLimits()
    network: NetworkMode = NetworkMode.NONE
    mounts: tuple[BindMount, ...] = ()
    #: 容器里的工作目录。挂了工作区就填 `WORKSPACE_TARGET`；
    #: 不填的话用镜像自己的默认值 —— 填一个镜像里不存在的目录，容器会起不来。
    workdir: str | None = None
    #: 环境变量。用 `build_env()` 拼，别手工塞。
    env: Mapping[str, str] = field(default_factory=dict)
    #: 容器里用哪个 `uid:gid`。不填按 `default_container_user()` 决定。
    user: str | None = None
    #: 额外的标签，会和 bench 自己的标签合并。
    labels: Mapping[str, str] = field(default_factory=dict)
    #: 超时后 `SIGTERM` 到 `SIGKILL` 之间的宽限期（秒）。
    #: 给 Agent 留一点时间把改了一半的文件落盘 —— 协议 C-09a 要求超时也要保存补丁。
    stop_grace_s: int = 10
    #: 这次评测的 id，写进容器标签，方便事后把残留容器对回具体哪一次运行。
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.timeout_s < 1:
            raise ValueError(f"timeout_s 至少 1 秒，收到 {self.timeout_s}")
        if self.stop_grace_s < 0:
            raise ValueError(f"stop_grace_s 不能是负数，收到 {self.stop_grace_s}")
        if not self.command:
            raise ValueError("command 不能为空")


@dataclass(frozen=True, slots=True)
class ContainerResult:
    """容器跑完之后我们知道的全部事实。

    注意这里只有**事实**，没有结论。"这次算不算平台故障"由 `classify_outcome()` 回答，
    "AI 有没有修好"要等判定引擎（E4-T3）看测试报告。
    """

    container_id: str
    image: str
    exit_code: int
    #: 来自 `docker inspect` 的 `.State.OOMKilled`。判内存超限**只能**看这个字段（协议 C-06）。
    oom_killed: bool
    #: 是我们主动把它杀掉的（墙钟到点），不是它自己退出的。
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    #: 日志超过 `MAX_LOG_BYTES` 被截断了。
    logs_truncated: bool = False

    @property
    def ok(self) -> bool:
        """正常退出：退出码 0，没 OOM，没超时。"""
        return self.exit_code == 0 and not self.oom_killed and not self.timed_out


# ══════════════════════════════════════════════════════════════
# 纯函数：环境变量、结果分类、时间戳
# ══════════════════════════════════════════════════════════════


def default_container_user() -> str:
    """容器里用哪个 `uid:gid` 跑。

    默认跟着当前进程走。工作区是宿主机上的目录、用绑定挂载给容器，uid 对不上的话
    容器写不进去 —— 报的是 `Permission denied`，很难一眼看出是 uid 的事。

    harness 自己是 root 时（Worker 跑在容器里就是这样）退到 nobody：§10.3 要求
    容器里不能是 root。这种部署下工作区目录必须事先 chown 给 nobody，否则一样写不进去。
    """
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return f"{NOBODY_UID}:{NOBODY_GID}"
    return f"{uid}:{gid}"


def build_env(
    requested: Mapping[str, str] | None = None,
    *,
    allowlist: Collection[str] = AGENT_ENV_ALLOWLIST,
) -> dict[str, str]:
    """拼出容器里的环境变量：固定的确定性变量 + 白名单里的请求项。

    容器**不会**继承宿主机环境，所以"显式清空其余"是天然成立的，这里要防的是反过来
    的两件事：

    1. 漏传一个 API Key —— Agent 跑起来才报 401，几分钟白花。
    2. 多传一个不该进去的变量 —— 比如 `GITHUB_TOKEN`，那等于把翻原 PR 的钥匙
       交给了被测 AI。

    所以名字不在白名单里就**抛错**，不是悄悄丢掉。悄悄丢掉的话，排查时根本想不到
    是这一层过滤的。

    确定性变量（`TZ`、`PYTHONHASHSEED` 等）不在白名单里，也就没法被覆盖 —— 这是故意的。
    """
    env = dict(DETERMINISM_ENV)
    for name, value in (requested or {}).items():
        if name not in allowlist:
            raise EnvNotAllowedError(name)
        env[name] = value
    return env


def classify_outcome(result: ContainerResult, *, stage: Stage) -> InfraOutcome:
    """把容器的结束方式翻译成 `infra_outcome`。

    **顺序不能换**：先看 `oom_killed`，再看 `timed_out`（协议 C-19b 第 1 步）。
    两种情况的退出码都是 137，反过来判会把内存超限当成超时 —— 前者按 C-18 要降配
    重试一次，后者直接判 AI 没修好，判反了排行榜就错了。

    非零退出码这里**不翻译**，直接当 SUCCESS 返回。因为它在两个阶段的含义相反：
    测试阶段非零退出是正常的（有用例失败，判定引擎会去读 junit 报告），Agent 阶段
    非零退出才算故障，而"Agent 算不算跑成功"要看它 stdout 最后一行的 JSON
    （Runner 协议，E3-T1），不是退出码。把它塞进这里会让沙箱层去猜上层的语义。
    """
    if result.oom_killed:
        return InfraOutcome.OOM_KILLED
    if result.timed_out:
        return InfraOutcome.AGENT_TIMEOUT if stage is Stage.AGENT else InfraOutcome.TEST_TIMEOUT
    return InfraOutcome.SUCCESS


def parse_docker_time(raw: str) -> datetime | None:
    """解析 docker 返回的时间戳，解析不了返回 None。

    docker 给的是纳秒精度（`2026-09-04T14:21:33.123456789Z`），而
    `datetime.fromisoformat` 只认 3 位或 6 位小数，纳秒会直接抛 ValueError。
    这里把小数截到 6 位再解析。

    容器从没启动过时这个字段是 `0001-01-01T00:00:00Z`，能解析出来但没意义，
    调用方要自己判断。
    """
    text = raw.strip()
    if not text:
        return None
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        suffix = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{suffix}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_capped(chunks: Iterable[bytes], limit: int) -> tuple[str, bool]:
    """把一串字节块拼成字符串，最多留 `limit` 个字节。

    返回 (文本, 是否被截断)。非法字节用替换字符兜住：容器可以输出任意字节，
    解码报错会让整次评测失败，而我们要的只是把日志存下来。
    """
    parts: list[bytes] = []
    remaining = limit
    truncated = False
    for chunk in chunks:
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            parts.append(chunk[:remaining])
            truncated = True
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts).decode("utf-8", errors="replace"), truncated


# ══════════════════════════════════════════════════════════════
# daemon 连接
# ══════════════════════════════════════════════════════════════

_client: Any = None


def get_docker_client() -> Any:
    """连本机 docker daemon，连上之后缓存起来。

    走 `docker.from_env()`：认 `DOCKER_HOST`，没设就用 `/var/run/docker.sock`。

    这台开发机上同时装了原生 dockerd 和 Docker Desktop，两边会抢这个 socket，
    所以别假设服务端是哪一个 —— `scripts/check_env.py` 会替你查。
    """
    global _client
    if _client is None:
        try:
            _client = docker.from_env(timeout=DOCKER_API_TIMEOUT_S)
        except DockerException as exc:
            raise DockerUnavailableError(f"连不上 docker daemon：{exc}") from exc
    return _client


def reset_docker_client() -> None:
    """丢掉缓存的连接。只在测试里用。"""
    global _client
    _client = None


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════


def _create_kwargs(spec: ContainerSpec) -> dict[str, Any]:
    """把 `ContainerSpec` 翻译成 docker SDK 的参数。

    加固项对应 §10.3「文件系统策略」一行：

    - `cap_drop=ALL`：丢掉全部 Linux capability。被测 AI 不需要改网卡、挂文件系统，
      给了反而是攻击面。
    - `no-new-privileges`：进程之后再也提不了权，setuid 程序失效。
    - 非 root 用户 + `/tmp` 用 tmpfs。

    tmpfs 上**不加** `noexec`：pip 和 uv 装包时会把构建脚本解到临时目录里执行，
    加了 noexec 会让一部分带 C 扩展的包装不上，而那不是我们要测的东西。
    """
    limits = spec.limits
    labels = {
        BENCH_LABEL: BENCH_LABEL_VALUE,
        BENCH_STAGE_LABEL: spec.stage.value,
        **({BENCH_RUN_LABEL: spec.run_id} if spec.run_id else {}),
        **dict(spec.labels),
    }
    kwargs: dict[str, Any] = {
        "image": spec.image,
        "command": list(spec.command),
        "labels": labels,
        "environment": dict(spec.env),
        "user": spec.user or default_container_user(),
        "network_mode": spec.network.value,
        # 内存和 swap 设成同一个值 = 禁用 swap，理由见 ResourceLimits.memory_mb
        "mem_limit": f"{limits.memory_mb}m",
        "memswap_limit": f"{limits.memory_mb}m",
        "nano_cpus": limits.nano_cpus,
        "pids_limit": limits.pids_limit,
        "tmpfs": {"/tmp": f"rw,nosuid,size={limits.tmpfs_mb}m,mode=1777"},
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        # 容器名默认是容器 id 的前 12 位，会出现在编译产物和某些测试的输出里。
        # 固定成一个常量，重跑两次的输出才可能逐字节相同
        "hostname": "bench",
        # 不能开 tty：开了 stdout 和 stderr 会合流，两条流就分不开了
        "tty": False,
        "detach": True,
    }
    if spec.workdir:
        kwargs["working_dir"] = spec.workdir
    if spec.mounts:
        kwargs["volumes"] = {
            str(m.source): {"bind": m.target, "mode": "ro" if m.read_only else "rw"}
            for m in spec.mounts
        }
    return kwargs


def run_in_container(spec: ContainerSpec, *, client: Any = None) -> ContainerResult:
    """在容器里跑一条命令，等它结束，把容器删掉。

    正常结束、被 OOM 杀掉、超时被我们杀掉，这三种情况都会**正常返回**一个
    `ContainerResult`，不抛异常 —— 它们是评测的正常输出，不是程序错误。
    抛异常的只有"沙箱自己坏了"：连不上 daemon、镜像不存在、容器起不来。

    容器一定会被删掉，包括抛异常的路径。
    """
    client = client or get_docker_client()
    container = None
    started = time.monotonic()
    try:
        try:
            container = client.containers.create(**_create_kwargs(spec))
        except ImageNotFound as exc:
            raise ImageNotFoundError(spec.image) from exc
        except DockerException as exc:
            raise SandboxError(f"创建容器失败（镜像 {spec.image}）：{exc}") from exc

        try:
            container.start()
        except DockerException as exc:
            raise SandboxError(f"启动容器失败（镜像 {spec.image}）：{exc}") from exc

        timed_out = False
        try:
            status = container.wait(timeout=spec.timeout_s)
            exit_code = int(status.get("StatusCode", -1))
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            # 墙钟到点。SDK 是靠 HTTP 读超时告诉我们的，两种异常都可能冒出来：
            # 连接池把读超时包成 ConnectionError 的情况实测存在
            timed_out = True
            exit_code = _stop_and_collect(container, spec.stop_grace_s)

        duration_s = time.monotonic() - started
        container.reload()
        state = container.attrs.get("State", {})
        stdout, out_cut = _read_stream(container, stdout=True)
        stderr, err_cut = _read_stream(container, stdout=False)
        return ContainerResult(
            container_id=str(container.id),
            image=spec.image,
            exit_code=exit_code,
            # 唯一可信的 OOM 判据（协议 C-06）。退出码在这里帮不上忙
            oom_killed=bool(state.get("OOMKilled", False)),
            timed_out=timed_out,
            duration_s=duration_s,
            stdout=stdout,
            stderr=stderr,
            logs_truncated=out_cut or err_cut,
        )
    finally:
        _remove_quietly(container)


def _stop_and_collect(container: Any, grace_s: int) -> int:
    """先 SIGTERM，宽限期到了再 SIGKILL，然后取退出码。

    `docker stop -t N` 就是这个语义，实测宽限期是准的（§10.3：设 2 秒，2,622 ms 后被杀）。
    不直接 kill 是因为协议 C-09a 要求超时也要保存 AI 已经改出来的补丁，
    得给它一点时间把文件落盘。
    """
    try:
        container.stop(timeout=grace_s)
    except NotFound:
        return -1
    except DockerException:
        # stop 失败就硬杀。杀不动的话下面 wait 会拿到实际状态
        try:
            container.kill()
        except DockerException:
            logger.warning("container_kill_failed", container_id=str(container.id))
    try:
        status = container.wait(timeout=grace_s + STOP_WAIT_SLACK_S)
    except (requests.exceptions.RequestException, DockerException):
        return -1
    return int(status.get("StatusCode", -1))


def _read_stream(container: Any, *, stdout: bool) -> tuple[str, bool]:
    """取容器的一条输出流，最多 `MAX_LOG_BYTES` 字节。

    分两次取而不是一次取合并流：合并之后就分不出哪句是 stderr 了，
    而"AI 报了什么错"几乎总是在 stderr 里。
    """
    try:
        chunks = container.logs(stdout=stdout, stderr=not stdout, stream=True, follow=False)
    except DockerException as exc:
        logger.warning("container_logs_failed", container_id=str(container.id), error=str(exc))
        return "", False
    try:
        return _read_capped(chunks, MAX_LOG_BYTES)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def _remove_quietly(container: Any) -> None:
    """删容器。删不掉只记一条日志，不抛异常。

    这个函数在 `finally` 里调用。要是它自己抛异常，会把真正的失败原因盖掉 ——
    调用方看到的是"删容器失败"，而不是"镜像不存在"。
    """
    if container is None:
        return
    try:
        container.remove(force=True)
    except NotFound:
        pass
    except (DockerException, requests.exceptions.RequestException) as exc:
        logger.warning("container_remove_failed", container_id=str(container.id), error=str(exc))


# ══════════════════════════════════════════════════════════════
# 孤儿回收
# ══════════════════════════════════════════════════════════════


def reap_orphans(*, client: Any = None, min_age_s: float = 0.0) -> list[str]:
    """删掉带 bench 标签的残留容器，返回被删掉的容器 id。

    Worker 启动时调一次。Worker 被 `kill -9` 的话 `run_in_container` 的 `finally`
    根本跑不到，容器会一直占着内存和 pid —— 下一批评测就会因为资源不够而莫名其妙
    地失败，而且失败原因指向的是新任务，不是那个已经死掉的 Worker。

    `min_age_s` 是保险丝：多个 Worker 同时跑时，别把别人**正在用**的容器删掉。
    传 0（默认）表示不管年龄全删，适合单机单 Worker。
    """
    client = client or get_docker_client()
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"}
        )
    except DockerException as exc:
        raise SandboxError(f"列容器失败：{exc}") from exc

    now = datetime.now(UTC)
    removed: list[str] = []
    for container in containers:
        if min_age_s > 0:
            created = parse_docker_time(str(container.attrs.get("Created", "")))
            if created is not None and (now - created).total_seconds() < min_age_s:
                continue
        container_id = str(container.id)
        _remove_quietly(container)
        removed.append(container_id)
    if removed:
        logger.info("reaped_orphan_containers", count=len(removed))
    return removed


def kill_containers_by_run_prefix(prefix: str, *, client: Any = None) -> list[str]:
    """杀掉 `bench.run_id` 标签以 `prefix` 开头的容器，返回被杀掉的容器 id。

    取消一次实验时用它（E5-T2）。取消的语义是"现在就停"，所以直接 `kill`
    而不是 `stop`：`stop` 要先 SIGTERM 再等宽限期，一道题最多能拖 10 秒，
    8 道题一起取消就顶掉了 30 秒验收窗口的四分之一还多。补丁在这里不用抢救——
    这次执行会被记成 `CANCELLED`，本来就不产出结论。

    **只 kill 不 remove。** 容器是 `run_in_container` 起的，它的 `finally`
    会把容器删掉；这里抢着删的话，那边紧接着的 `container.reload()` 会撞上
    404，一次干净的取消就变成一条 `HARNESS_ERROR` 了。

    docker 的标签过滤只能精确匹配，做不了前缀匹配，所以是先按 bench 标签
    把容器列出来，再在 Python 里筛前缀。一台机器上同时也就几十个容器。
    """
    client = client or get_docker_client()
    try:
        containers = client.containers.list(filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"})
    except DockerException as exc:
        raise SandboxError(f"列容器失败：{exc}") from exc

    killed: list[str] = []
    for container in containers:
        run_id = str((container.labels or {}).get(BENCH_RUN_LABEL, ""))
        if not run_id.startswith(prefix):
            continue
        try:
            container.kill()
        except NotFound:
            continue  # 已经自己结束了，正是我们想要的
        except DockerException as exc:
            logger.warning("cancel_kill_failed", container_id=str(container.id), error=str(exc))
            continue
        killed.append(str(container.id))
    if killed:
        logger.warning("cancel_killed_containers", prefix=prefix, count=len(killed))
    return killed


__all__ = [
    "AGENT_ENV_ALLOWLIST",
    "BENCH_LABEL",
    "BENCH_LABEL_VALUE",
    "BENCH_RUN_LABEL",
    "BENCH_STAGE_LABEL",
    "DETERMINISM_ENV",
    "MAX_LOG_BYTES",
    "WORKSPACE_TARGET",
    "BindMount",
    "ContainerResult",
    "ContainerSpec",
    "DockerUnavailableError",
    "EnvNotAllowedError",
    "ImageNotFoundError",
    "NetworkMode",
    "ResourceLimits",
    "SandboxError",
    "Stage",
    "build_env",
    "classify_outcome",
    "default_container_user",
    "get_docker_client",
    "kill_containers_by_run_prefix",
    "parse_docker_time",
    "reap_orphans",
    "reset_docker_client",
    "run_in_container",
]
