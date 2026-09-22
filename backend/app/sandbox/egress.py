"""出站白名单网络与代理容器的管理（E2-T4，`05-sandbox.md` §10.5）。

## 结构

    ┌──────────────────────── docker 网络 bench-egress（internal）────────────────────┐
    │  Agent 容器 ──HTTP_PROXY=http://bench-egress-proxy:3128──▶ bench-egress-proxy  │
    └────────────────────────────────────────────────────────────────────┬──────────┘
                                                                         │ 也接在默认 bridge 上
                                                                         ▼
                                                        只对名单里的域名做 CONNECT 转发

`internal` 网络没有网关：Agent 容器上 `curl https://140.82.112.3` 会直接
"Network is unreachable"，域名规则根本用不着——这就是"直连 IP 也被阻断"的来源。
代理容器同时接两个网络，它是这个笼子唯一的门，门上只认名单。

## 为什么 Agent 容器不接 bridge、再靠 iptables 拦

iptables 规则在 DooD 模式下要改宿主机，compose 部署做不到；而且规则漏一条就是
静默放行，没人会发现。`internal` 是 docker 自己保证的，少一层我们自己维护的东西。

## 代理容器必须**改写** `bench.owner` 标签

`reap_orphans()` 在 Worker 启动时会删掉所有 `bench.owner=coding-agent-benchmark` 的容器。
光"不加这个标签"不够：`bench-base:py311` 镜像里烤着这个标签，容器会**继承**镜像的标签
（2026-09-22 实测：Worker 一启动就把刚起的代理删了，`reaped_orphan_containers count=1`）。
所以这里显式把 `bench.owner` 写成另一个值，容器标签覆盖镜像标签，回收器就认不出它。
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException, ImageNotFound, NotFound

from app.infrastructure.config import (
    EGRESS_NETWORK_DEFAULT,
    EGRESS_PROXY_CONTAINER,
    EGRESS_PROXY_PORT,
)
from app.infrastructure.logging import get_logger
from app.sandbox.container import (
    BENCH_LABEL,
    ContainerSpec,
    ImageNotFoundError,
    NetworkMode,
    ResourceLimits,
    SandboxError,
    Stage,
    default_container_user,
    get_docker_client,
    run_in_container,
)

logger = get_logger(__name__)

EGRESS_NETWORK = EGRESS_NETWORK_DEFAULT
PROXY_CONTAINER = EGRESS_PROXY_CONTAINER
PROXY_PORT = EGRESS_PROXY_PORT
#: 代理容器和探针默认用的镜像：有 Python 3.11 和 curl，而且本来就在每台评测机上。
DEFAULT_IMAGE = "bench-base:py311"
ROLE_LABEL = "bench.role"
ROLE_PROXY = "egress-proxy"
#: 覆盖镜像里的 `bench.owner`，让 `reap_orphans()` 的过滤器（等值匹配）认不出它
OWNER_PROXY = "coding-agent-benchmark-egress"
NETWORK_LABEL = "bench.network"


def proxy_labels(network: str = EGRESS_NETWORK) -> dict[str, str]:
    """代理容器的标签。`bench.owner` 必须覆盖成非 bench 的值，理由见模块开头。"""
    return {BENCH_LABEL: OWNER_PROXY, ROLE_LABEL: ROLE_PROXY, NETWORK_LABEL: network}


def proxy_url(container: str = PROXY_CONTAINER, port: int = PROXY_PORT) -> str:
    return f"http://{container}:{port}"


def proxy_program() -> str:
    """代理程序的源码。整段用 `python -c` 塞进容器，不需要镜像或挂载。"""
    return importlib.resources.files("app.sandbox").joinpath("egress_proxy.py").read_text("utf-8")


# ── 网络 ─────────────────────────────────────────────────────


def ensure_network(name: str = EGRESS_NETWORK, *, client: Any = None) -> bool:
    """建 internal 网络；已存在就校验它确实是 internal。返回是否新建。

    已存在但**不是** internal 的网络直接抛错而不是复用：那样的网络有网关，
    Agent 容器能直连出去，整个白名单就是摆设。
    """
    client = client or get_docker_client()
    try:
        network = client.networks.get(name)
    except NotFound:
        network = None
    except DockerException as exc:
        raise SandboxError(f"查网络 {name} 失败：{exc}") from exc
    if network is not None:
        if not bool(network.attrs.get("Internal", False)):
            raise SandboxError(
                f"docker 网络 {name} 已存在但不是 internal 的；删掉它再重建："
                f"docker network rm {name}"
            )
        return False
    try:
        client.networks.create(name, driver="bridge", internal=True, labels={NETWORK_LABEL: name})
    except DockerException as exc:
        raise SandboxError(f"建网络 {name} 失败：{exc}") from exc
    logger.info("egress_network_created", network=name)
    return True


# ── 代理容器 ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProxyStatus:
    running: bool
    container_id: str | None = None
    allow: str | None = None
    upstream: str | None = None
    networks: tuple[str, ...] = ()


def _find_proxy(client: Any, name: str) -> Any | None:
    try:
        return client.containers.get(name)
    except NotFound:
        return None
    except DockerException as exc:
        raise SandboxError(f"查容器 {name} 失败：{exc}") from exc


def proxy_status(*, name: str = PROXY_CONTAINER, client: Any = None) -> ProxyStatus:
    client = client or get_docker_client()
    container = _find_proxy(client, name)
    if container is None:
        return ProxyStatus(running=False)
    env = {
        k: v
        for k, _, v in (item.partition("=") for item in container.attrs["Config"].get("Env", []))
    }
    return ProxyStatus(
        running=container.status == "running",
        container_id=str(container.id),
        allow=env.get("EGRESS_ALLOW"),
        upstream=env.get("EGRESS_UPSTREAM") or None,
        networks=tuple(container.attrs["NetworkSettings"]["Networks"].keys()),
    )


def start_proxy(
    *,
    allow: str,
    upstream: str | None = None,
    network: str = EGRESS_NETWORK,
    name: str = PROXY_CONTAINER,
    image: str = DEFAULT_IMAGE,
    client: Any = None,
) -> str:
    """起（或重建）代理容器，接在 `network` 和默认 bridge 上。返回容器 id。

    名单变了必须重建：名单是环境变量，容器起来之后改不了。所以这里的做法是
    有旧的就先删。`restart_policy=always` 让它跟着 dockerd 一起起来，
    Worker 重启不需要人再管它。
    """
    if not allow.strip():
        raise SandboxError("白名单是空的，代理不会起：它起来也只会拒绝所有连接")
    client = client or get_docker_client()
    ensure_network(network, client=client)
    old = _find_proxy(client, name)
    if old is not None:
        try:
            old.remove(force=True)
        except DockerException as exc:
            raise SandboxError(f"删旧代理容器失败：{exc}") from exc

    env = {"EGRESS_ALLOW": allow, "EGRESS_PORT": str(PROXY_PORT)}
    if upstream:
        env["EGRESS_UPSTREAM"] = upstream
    try:
        container = client.containers.create(
            image=image,
            name=name,
            command=["python", "-c", proxy_program()],
            environment=env,
            labels=proxy_labels(network),
            user=default_container_user(),
            network=network,
            hostname=name,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="256m",
            pids_limit=256,
            restart_policy={"Name": "always"},
            detach=True,
        )
        # 第二条腿：接到默认 bridge，代理才有出网的路。Agent 容器只在 internal 网络里
        client.networks.get("bridge").connect(container)
        container.start()
    except ImageNotFound as exc:
        raise ImageNotFoundError(image) from exc
    except DockerException as exc:
        raise SandboxError(f"起代理容器失败：{exc}") from exc
    logger.info("egress_proxy_started", container_id=container.id, allow=allow, upstream=upstream)
    return str(container.id)


def stop_proxy(*, name: str = PROXY_CONTAINER, client: Any = None) -> bool:
    """删掉代理容器。返回是否真有东西被删。网络留着，空网络不占资源。"""
    client = client or get_docker_client()
    container = _find_proxy(client, name)
    if container is None:
        return False
    try:
        container.remove(force=True)
    except DockerException as exc:
        raise SandboxError(f"删代理容器失败：{exc}") from exc
    return True


def proxy_logs(*, name: str = PROXY_CONTAINER, tail: int = 50, client: Any = None) -> str:
    client = client or get_docker_client()
    container = _find_proxy(client, name)
    if container is None:
        return ""
    return str(container.logs(tail=tail).decode("utf-8", "replace"))


# ── 验收探针 ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProbeCase:
    """一条验收检查：在 Agent 同款网络里跑一条 curl，看它是不是按预期通/不通。"""

    key: str
    title: str
    command: str
    #: True = 必须连得上（curl 退出码 0）；False = 必须连不上
    expect_reachable: bool


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    case: ProbeCase
    exit_code: int
    detail: str

    @property
    def passed(self) -> bool:
        # 探针没输出这一条（-1）不算通过——"没连上"和"没测"必须分开
        if self.exit_code == -1:
            return False
        return (self.exit_code == 0) == self.case.expect_reachable


def probe_cases(allow_host: str) -> tuple[ProbeCase, ...]:
    """E2-T4 的验收标准，外加两条 2026-09-22 之后必须有的：直连 IP 与绕开代理。"""
    return (
        ProbeCase(
            "github_via_proxy",
            "过代理访问 github.com 必须被拒",
            "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://github.com/",
            expect_reachable=False,
        ),
        ProbeCase(
            "github_direct",
            "绕开代理直连 github.com 必须不通（网络没有出口）",
            "curl -sS -m 20 --noproxy '*' -o /dev/null -w '%{http_code}' https://github.com/",
            expect_reachable=False,
        ),
        ProbeCase(
            "ip_direct",
            "直连 IP（140.82.112.3，GitHub）必须不通",
            "curl -sS -m 20 --noproxy '*' -o /dev/null -w '%{http_code}' https://140.82.112.3/",
            expect_reachable=False,
        ),
        ProbeCase(
            "ip_via_proxy",
            "过代理 CONNECT 到裸 IP 必须被拒",
            "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://140.82.112.3/",
            expect_reachable=False,
        ),
        ProbeCase(
            "llm_api",
            f"过代理访问 {allow_host} 必须通（拿到任意 HTTP 状态码即可）",
            f"curl -sS -m 30 -o /dev/null -w '%{{http_code}}' https://{allow_host}/",
            expect_reachable=True,
        ),
    )


def _probe_script(cases: tuple[ProbeCase, ...]) -> str:
    lines = ["set +e"]
    for case in cases:
        lines.append(f"echo '### {case.key}'")
        lines.append(f"out=$({case.command} 2>&1); code=$?")
        lines.append(f'echo "{case.key} exit=$code out=$out"')
    return "\n".join(lines)


def parse_probe_output(stdout: str, cases: tuple[ProbeCase, ...]) -> list[ProbeOutcome]:
    results: list[ProbeOutcome] = []
    for case in cases:
        prefix = f"{case.key} exit="
        line = next((ln for ln in stdout.splitlines() if ln.startswith(prefix)), None)
        if line is None:
            results.append(ProbeOutcome(case, -1, "探针没有输出这一条"))
            continue
        rest = line[len(prefix) :]
        code_text, _, detail = rest.partition(" out=")
        try:
            code = int(code_text)
        except ValueError:
            code = -1
        results.append(ProbeOutcome(case, code, detail.strip()))
    return results


def run_probe(
    *,
    allow_host: str,
    network: str = EGRESS_NETWORK,
    proxy: str | None = None,
    image: str = DEFAULT_IMAGE,
    client: Any = None,
) -> list[ProbeOutcome]:
    """在和 Agent 完全相同的网络设置下起一个容器跑五条 curl。

    用的就是 `run_in_container()` + `NetworkMode.EGRESS`，和真实评测走同一条路——
    验收验的是评测时的那个笼子，不是另一个"看起来一样"的容器。
    """
    cases = probe_cases(allow_host)
    proxy = proxy or proxy_url()
    env = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": "",
        "no_proxy": "",
    }
    spec = ContainerSpec(
        image=image,
        command=["bash", "-c", _probe_script(cases)],
        timeout_s=180,
        stage=Stage.AGENT,
        limits=ResourceLimits(),
        network=NetworkMode.EGRESS,
        network_name=network,
        env=env,
        run_id="egress-probe",
    )
    result = run_in_container(spec, client=client)
    outcomes = parse_probe_output(result.stdout, cases)
    if all(o.exit_code == -1 for o in outcomes):
        raise SandboxError(
            f"探针容器没有输出（退出码 {result.exit_code}）：\n{result.stderr[-2000:]}"
        )
    return outcomes


def format_probe(outcomes: list[ProbeOutcome]) -> str:
    lines = []
    for o in outcomes:
        mark = "✅" if o.passed else "❌"
        lines.append(f"{mark} {o.case.title}")
        lines.append(f"     exit={o.exit_code} {o.detail[:160]}")
    return "\n".join(lines)
