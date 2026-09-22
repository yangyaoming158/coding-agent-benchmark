"""出站白名单代理与网络选择（E2-T4）。

代理程序在本机起一个线程、用真 socket 打，不 mock：它要拦的是被测 AI 的真实 `curl`，
mock 掉 socket 层就等于没测。需要 docker 的部分（建网络、起容器、五条探针）
在 `cli.egress check` 里，属于验收步骤，不在这里。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from app.infrastructure.config import EGRESS_PROXY_URL_DEFAULT, Settings
from app.runner.adapters.claude_code import DISALLOWED_TOOLS, build_command
from app.runner.adapters.network import agent_network
from app.runner.protocol import AgentConfig
from app.sandbox import egress
from app.sandbox.container import ContainerSpec, NetworkMode, _create_kwargs
from app.sandbox.egress_proxy import (
    EgressProxyServer,
    host_allowed,
    parse_allowlist,
    parse_connect_target,
    parse_upstream,
    serve_in_thread,
)
from tests.contract.runner_contract import make_task_input as _make_task_input


def make_task_input():  # type: ignore[no-untyped-def]
    return _make_task_input(deadline_ms=4_000_000_000_000)


# ── 纯函数 ──────────────────────────────────────────────────


def test_allowlist_parsing_normalizes_case_dots_and_separators() -> None:
    assert parse_allowlist(" API.deepseek.com, .dashscope.aliyuncs.com  api.deepseek.com") == (
        "api.deepseek.com",
        "dashscope.aliyuncs.com",
    )
    assert parse_allowlist("") == ()


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("api.deepseek.com", True),
        ("API.DEEPSEEK.COM.", True),
        ("v2.api.deepseek.com", True),  # 子域名
        ("deepseek.com", False),  # 父域名不算
        ("api.deepseek.com.attacker.io", False),  # 前缀相同但不是子域名
        ("notapi.deepseek.com", False),
        ("140.82.112.3", False),  # 裸 IP 永远不在名单里
        ("", False),
    ],
)
def test_host_allowed_is_exact_or_subdomain_only(host: str, ok: bool) -> None:
    assert host_allowed(host, ("api.deepseek.com",)) is ok


def test_connect_target_parsing() -> None:
    assert parse_connect_target("CONNECT api.deepseek.com:443 HTTP/1.1") == (
        "api.deepseek.com",
        443,
    )
    assert parse_connect_target("connect [::1]:443 HTTP/1.1") == ("::1", 443)
    assert parse_connect_target("GET http://x/ HTTP/1.1") is None
    assert parse_connect_target("CONNECT host:abc HTTP/1.1") is None


def test_upstream_parsing() -> None:
    assert parse_upstream(None) is None
    assert parse_upstream("") is None
    assert parse_upstream("http://172.30.80.1:10808/") == ("172.30.80.1", 10808)
    assert parse_upstream("proxy:8080") == ("proxy", 8080)


# ── 真 socket 打真代理 ────────────────────────────────────────


@pytest.fixture
def target() -> Iterator[tuple[int, list[bytes]]]:
    """一个假的"API 服务器"：收到什么记下来，回一句话。"""
    received: list[bytes] = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                received.append(conn.recv(1024))
                conn.sendall(b"hello-from-target")

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], received
    srv.close()


@pytest.fixture
def proxy() -> Iterator[EgressProxyServer]:
    # 名单里放 localhost：CONNECT localhost:443 会被放行、但连不上（没人监听 443），
    # 用来验证"放行了但上游失败"的 502；真正的转发用 mock 掉的上游
    server = serve_in_thread(parse_allowlist("api.deepseek.com,localhost"))
    yield server
    server.shutdown()
    server.server_close()


def _connect(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        return sock.recv(4096)


def test_proxy_denies_everything_outside_the_allowlist(proxy: EgressProxyServer) -> None:
    port = proxy.server_address[1]
    for request in (
        b"CONNECT github.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 140.82.112.3:443 HTTP/1.1\r\n\r\n",  # 裸 IP
        b"CONNECT api.deepseek.com:80 HTTP/1.1\r\n\r\n",  # 名单内但不是 443
        b"GET http://api.deepseek.com/ HTTP/1.1\r\nHost: api.deepseek.com\r\n\r\n",  # 明文 HTTP
        b"garbage\r\n\r\n",
    ):
        assert _connect(port, request).startswith(b"HTTP/1.1 403"), request


def test_proxy_answers_502_when_allowed_host_is_unreachable(proxy: EgressProxyServer) -> None:
    """放行的域名连不上是上游的问题，要和"被拒"分得开：客户端看到 502 不是 403。"""
    port = proxy.server_address[1]
    reply = _connect(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 502")


def test_proxy_tunnels_allowed_host_end_to_end(
    proxy: EgressProxyServer, target: tuple[int, list[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """名单内的 CONNECT：先回 200，之后字节原样双向转发。

    真实目标是 443 端口，这里把"连上游"换成连本机的假服务器；判"在不在名单里"
    那一段没有被换掉。
    """
    from app.sandbox import egress_proxy

    target_port, received = target
    real = socket.create_connection

    def to_local(address: tuple[str, int], timeout: float | None = None) -> socket.socket:
        # 同一个 socket 模块，测试自己连代理也会经过这里：只改写去 API 的那一条
        if address == ("api.deepseek.com", 443):
            return real(("127.0.0.1", target_port), timeout=timeout)
        return real(address, timeout=timeout)

    monkeypatch.setattr(egress_proxy.socket, "create_connection", to_local)
    port = proxy.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: api.deepseek.com:443\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.1 200")
        sock.sendall(b"ping-through-tunnel")
        assert sock.recv(64) == b"hello-from-target"
    assert received == [b"ping-through-tunnel"]


# ── 容器规格 / 适配器怎么选网络 ──────────────────────────────


def test_egress_spec_requires_a_network_name() -> None:
    with pytest.raises(ValueError, match="network_name"):
        ContainerSpec(image="i", command=["true"], timeout_s=5, network=NetworkMode.EGRESS)


def test_egress_spec_uses_the_network_name_as_docker_network_mode() -> None:
    spec = ContainerSpec(
        image="i",
        command=["true"],
        timeout_s=5,
        network=NetworkMode.EGRESS,
        network_name="bench-egress",
    )
    assert _create_kwargs(spec)["network_mode"] == "bench-egress"


def test_agent_network_prefers_egress_and_never_bridge_when_configured() -> None:
    task = make_task_input()
    assert agent_network(task, AgentConfig(egress_network="bench-egress")) == (
        NetworkMode.EGRESS,
        "bench-egress",
    )
    # 没配白名单网络才退回 BRIDGE——只准开发机调试
    assert agent_network(task, AgentConfig()) == (NetworkMode.BRIDGE, None)
    offline = task.model_copy(
        update={"constraints": task.constraints.model_copy(update={"allow_network": False})}
    )
    assert agent_network(offline, AgentConfig(egress_network="bench-egress")) == (
        NetworkMode.NONE,
        None,
    )


# ── 配置：Agent 容器拿到的代理地址 ────────────────────────────


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_agent_env_points_at_the_egress_proxy_by_default() -> None:
    env = _settings(DEEPSEEK_API_KEY="k").agent_env_for("deepseek-flash")
    assert env["HTTPS_PROXY"] == env["https_proxy"] == EGRESS_PROXY_URL_DEFAULT
    # docker 客户端会往容器注一份自己的 NO_PROXY，这里显式清空，免得名单外的域名被它放行直连
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""


def test_explicit_sandbox_proxy_wins_and_blank_network_means_direct() -> None:
    assert _settings(SANDBOX_HTTP_PROXY="http://x:1").agent_proxy_url() == "http://x:1"
    direct = _settings(SANDBOX_EGRESS_NETWORK="")
    assert direct.sandbox_egress_network is None
    assert direct.agent_proxy_url() is None
    assert "HTTPS_PROXY" not in direct.agent_env_for("deepseek-flash")


# ── claude-code 必须关掉联网工具 ──────────────────────────────


def test_claude_code_disables_web_tools_as_one_comma_joined_argument() -> None:
    """`--disallowedTools` 是变长开关：值必须是一个参数，否则会把 `--model` 和提示词都吞掉。"""
    command = build_command(make_task_input(), "m", max_turns=3)
    i = command.index("--disallowedTools")
    assert command[i + 1] == "WebFetch,WebSearch"
    assert command[i + 2] == "--model"
    assert set(DISALLOWED_TOOLS) == {"WebFetch", "WebSearch"}


# ── 探针输出解析 ────────────────────────────────────────────


def test_probe_output_parsing_and_pass_criteria() -> None:
    cases = egress.probe_cases("api.deepseek.com")
    stdout = "\n".join(
        [
            "### github_via_proxy",
            "github_via_proxy exit=56 out=curl: (56) CONNECT tunnel failed, response 403",
            "github_direct exit=6 out=curl: (6) Could not resolve host: github.com",
            "ip_direct exit=7 out=curl: (7) Failed to connect",
            "ip_via_proxy exit=56 out=curl: (56) CONNECT tunnel failed, response 403",
            "llm_api exit=0 out=401",
        ]
    )
    outcomes = egress.parse_probe_output(stdout, cases)
    assert [o.passed for o in outcomes] == [True] * 5
    # 反过来：github 通了就是不通过，API 不通也是不通过
    bad = stdout.replace("github_via_proxy exit=56", "github_via_proxy exit=0").replace(
        "llm_api exit=0", "llm_api exit=7"
    )
    assert [o.passed for o in egress.parse_probe_output(bad, cases)] == [
        False,
        True,
        True,
        True,
        False,
    ]
    # 缺一条就按 -1 记，而且不算通过："没测"不能冒充"没连上"
    missing = egress.parse_probe_output("llm_api exit=0 out=401", cases)
    assert missing[0].exit_code == -1 and missing[0].passed is False
    assert missing[4].passed is True
