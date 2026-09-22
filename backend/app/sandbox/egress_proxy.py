"""出站白名单代理（E2-T4）—— 在代理容器里跑的那段程序。

Agent 容器接在一个 `internal` 的 docker 网络上，自己没有任何出网的路；唯一的出口是
这段程序：它接在同一个网络上，同时也接在默认桥接上，只替名单里的域名转发。

## 只做 `CONNECT`

大模型 API 全是 HTTPS，客户端过代理时发的是 `CONNECT host:443`。这里只认这一种：
名单里的放行，其余一律 `403`；普通的 `GET http://…` 也直接 `403`——明文 HTTP 没有
任何一家 API 会用，放开它只会多一条漏网的路。

判"在不在名单里"看的是 `CONNECT` 行里的主机名，**不是**解析后的 IP：
`CONNECT 140.82.112.3:443`（GitHub 的直连 IP）不在名单里，同样拒绝。

## 为什么自己写而不用 tinyproxy / squid

这台机器拉一个 alpine 镜像要走境外代理，2026-09-22 实测两分钟没拉完；而
`bench-base:py311` 本来就有 Python。这段程序**只用标准库**，整段源码由
`app.sandbox.egress` 读出来、用 `python -c` 塞进容器，不需要新镜像、不需要挂载。

## 日志就是取证材料

每个连接一行 `ALLOW host:port` 或 `DENY host:port reason`，写到 stdout。
`cli.egress check` 靠它验收；出了"AI 是不是看过原 PR"的争议时也先翻它。

**这个文件不能 import 项目里的任何东西**（会被当成独立脚本执行）。
"""

from __future__ import annotations

import contextlib
import os
import select
import socket
import socketserver
import sys
import threading
import time

#: 代理监听的端口。大于 1024，容器里不用 root 也能监听。
DEFAULT_PORT = 3128
#: 允许 CONNECT 的目标端口。只放 443：名单里的域名开别的端口没有任何正当理由。
ALLOWED_PORTS = frozenset({443})
#: 读请求头 / 连上游的超时（秒）。太长的话被测 AI 一次失败的连接会占住线程很久。
HANDSHAKE_TIMEOUT_S = 15.0
#: 隧道里双方都没有数据的最长时间（秒）。大模型一次流式回复可能几分钟没字节，
#: 所以不能太短；10 分钟够 claude / aider 的单次请求超时先到。
IDLE_TIMEOUT_S = 600.0
_BUFFER = 65536
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def parse_allowlist(raw: str) -> tuple[str, ...]:
    """把 `a.com, b.org` 这种逗号/空白分隔的字符串拆成小写域名元组，去掉前导点。"""
    names: list[str] = []
    for chunk in raw.replace(",", " ").split():
        name = chunk.strip().strip(".").lower()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def host_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    """`host` 是名单里的域名本身或它的子域名才算放行。

    只做字符串匹配，故意不解析 DNS：解析了就等于替被测 AI 把 IP 找出来，
    而 IP 字面量（`140.82.112.3`）永远不会等于任何一个域名。
    """
    host = host.strip().strip(".").lower()
    if not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowlist)


def parse_connect_target(request_line: str) -> tuple[str, int] | None:
    """从 `CONNECT host:port HTTP/1.1` 里取出 (host, port)。不是 CONNECT 就返回 None。"""
    parts = request_line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    target = parts[1]
    if target.startswith("["):  # IPv6 字面量 [::1]:443
        host, _, rest = target[1:].partition("]")
        port_text = rest.lstrip(":")
    else:
        host, _, port_text = target.rpartition(":")
        if not host:
            host, port_text = target, "443"
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host, port


def parse_upstream(raw: str | None) -> tuple[str, int] | None:
    """`host:port` 或 `http://host:port` → (host, port)。空的就是直连。"""
    if not raw:
        return None
    text = raw.strip()
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.rstrip("/")
    host, _, port_text = text.rpartition(":")
    if not host:
        return text, 3128
    return host, int(port_text)


def _log(line: str) -> None:
    sys.stdout.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    sys.stdout.flush()


def _read_headers(conn: socket.socket) -> bytes:
    """读到空行为止；头太长或对方断开就返回读到的部分。"""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 16384:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _pipe(a: socket.socket, b: socket.socket, idle_timeout_s: float) -> None:
    """双向转发，直到一边关闭或双方都空闲超过 `idle_timeout_s`。"""
    sockets = [a, b]
    peer = {a: b, b: a}
    while True:
        readable, _, errored = select.select(sockets, [], sockets, idle_timeout_s)
        if errored or not readable:
            return
        for src in readable:
            try:
                data = src.recv(_BUFFER)
            except OSError:
                return
            if not data:
                return
            try:
                peer[src].sendall(data)
            except OSError:
                return


class _Handler(socketserver.BaseRequestHandler):
    """一个连接一个线程。`server` 上挂着 allowlist / upstream。"""

    def handle(self) -> None:
        conn: socket.socket = self.request
        server: EgressProxyServer = self.server  # type: ignore[assignment]
        conn.settimeout(HANDSHAKE_TIMEOUT_S)
        try:
            head = _read_headers(conn)
        except OSError:
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        target = parse_connect_target(request_line)
        if target is None:
            _log(f"DENY {request_line[:80]!r} not-connect")
            self._reply(conn, _FORBIDDEN)
            return
        host, port = target
        label = f"{host}:{port}"
        if port not in ALLOWED_PORTS:
            _log(f"DENY {label} port")
            self._reply(conn, _FORBIDDEN)
            return
        if not host_allowed(host, server.allowlist):
            _log(f"DENY {label} not-in-allowlist")
            self._reply(conn, _FORBIDDEN)
            return

        try:
            upstream = self._open_upstream(server, host, port)
        except OSError as exc:
            _log(f"DENY {label} upstream-failed {exc}")
            self._reply(conn, _BAD_GATEWAY)
            return

        _log(f"ALLOW {label}")
        try:
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            conn.settimeout(None)
            upstream.settimeout(None)
            _pipe(conn, upstream, server.idle_timeout_s)
        except OSError:
            pass
        finally:
            upstream.close()

    @staticmethod
    def _reply(conn: socket.socket, payload: bytes) -> None:
        with contextlib.suppress(OSError):
            conn.sendall(payload)

    @staticmethod
    def _open_upstream(server: EgressProxyServer, host: str, port: int) -> socket.socket:
        """直连目标；配了上游代理就先对上游发一次 CONNECT（链式代理）。"""
        if server.upstream is None:
            return socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT_S)
        sock = socket.create_connection(server.upstream, timeout=HANDSHAKE_TIMEOUT_S)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        status = _read_headers(sock).split(b"\r\n", 1)[0]
        if b" 200" not in status:
            sock.close()
            raise OSError(f"upstream answered {status.decode('latin-1', 'replace')!r}")
        return sock


class EgressProxyServer(socketserver.ThreadingTCPServer):
    """多线程 TCP 服务；每个连接一个线程，主线程只 accept。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        allowlist: tuple[str, ...],
        upstream: tuple[str, int] | None = None,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ) -> None:
        self.allowlist = allowlist
        self.upstream = upstream
        self.idle_timeout_s = idle_timeout_s
        super().__init__(address, _Handler)


def serve_in_thread(
    allowlist: tuple[str, ...],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    upstream: tuple[str, int] | None = None,
) -> EgressProxyServer:
    """起一个后台线程里的代理，返回服务对象（`server_address` 里有实际端口）。给测试用。"""
    server = EgressProxyServer((host, port), allowlist=allowlist, upstream=upstream)
    thread = threading.Thread(target=server.serve_forever, name="egress-proxy", daemon=True)
    thread.start()
    return server


def main() -> None:
    """容器入口。配置全走环境变量，因为整段源码是用 `python -c` 塞进去的，没有参数位。"""
    allowlist = parse_allowlist(os.environ.get("EGRESS_ALLOW", ""))
    upstream = parse_upstream(os.environ.get("EGRESS_UPSTREAM"))
    port = int(os.environ.get("EGRESS_PORT", str(DEFAULT_PORT)))
    if not allowlist:
        _log("FATAL EGRESS_ALLOW is empty; refusing to start a proxy that allows nothing")
        sys.exit(2)
    server = EgressProxyServer(("0.0.0.0", port), allowlist=allowlist, upstream=upstream)
    _log(f"START port={port} allow={','.join(allowlist)} upstream={upstream or 'direct'}")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
