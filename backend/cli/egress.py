"""出站白名单网络与代理的命令行（E2-T4，`05-sandbox.md` §10.5）。

    python -m cli.egress up        # 建 internal 网络 + 起代理容器（名单从 .env 来）
    python -m cli.egress status    # 代理在不在、名单是什么、接了哪些网络
    python -m cli.egress check     # 验收：在 Agent 同款网络里跑五条 curl
    python -m cli.egress logs      # 代理最近的 ALLOW / DENY 记录
    python -m cli.egress down      # 删代理容器（网络留着）

**跑正式实验前先 `check`。** 五条全 ✅ 才算笼子是关着的；任何一条 ❌ 都说明
被测 AI 可能碰到互联网，那一轮的结果不能进排行榜。

名单改了（`.env` 的 `SANDBOX_EGRESS_ALLOW`）要重新 `up`：名单是容器的环境变量，
起来之后改不了。
"""

from __future__ import annotations

import argparse

from app.infrastructure.config import Settings, get_settings
from app.sandbox import egress
from app.sandbox.container import SandboxError


def _network_or_fail(settings: Settings) -> str | None:
    network = settings.sandbox_egress_network
    if not network:
        print("`.env` 里 SANDBOX_EGRESS_NETWORK 是空的：Agent 容器会直连互联网，没有笼子可管。")
        print("要启用就把它删掉（用默认值 bench-egress）或填一个名字。")
    return network


def cmd_up(args: argparse.Namespace) -> int:
    settings = get_settings()
    network = _network_or_fail(settings)
    if not network:
        return 1
    allow = args.allow or settings.sandbox_egress_allow
    upstream = args.upstream or settings.sandbox_egress_upstream
    container_id = egress.start_proxy(
        allow=allow, upstream=upstream, network=network, image=args.image
    )
    print(f"网络 {network}（internal）已就绪")
    print(f"代理 {egress.PROXY_CONTAINER} 已起（{container_id[:12]}）")
    print(f"  放行：{allow}")
    print(f"  上游：{upstream or '直连'}")
    print(f"  Agent 容器会拿到 HTTP_PROXY={settings.agent_proxy_url()}")
    print("下一步：python -m cli.egress check")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    removed = egress.stop_proxy()
    print("代理容器已删" if removed else "没有代理容器，什么都没做")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    status = egress.proxy_status()
    print(
        f"配置：网络={settings.sandbox_egress_network or '（空，直连）'}"
        f" 放行={settings.sandbox_egress_allow}"
        f" 上游={settings.sandbox_egress_upstream or '直连'}"
    )
    if not status.running:
        state = "存在但没在跑" if status.container_id else "不存在"
        print(f"代理容器：{state} → python -m cli.egress up")
        return 1
    print(f"代理容器：运行中（{(status.container_id or '')[:12]}）")
    print(f"  放行：{status.allow}")
    print(f"  上游：{status.upstream or '直连'}")
    print(f"  网络：{', '.join(status.networks)}")
    if status.allow != settings.sandbox_egress_allow:
        print("  ⚠ 容器里的名单和 .env 不一致，重新 `up` 一次")
        return 1
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    text = egress.proxy_logs(tail=args.tail)
    print(text.rstrip() if text else "（没有代理容器）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    network = _network_or_fail(settings)
    if not network:
        return 1
    status = egress.proxy_status()
    if not status.running:
        print("代理容器没在跑，先 python -m cli.egress up")
        return 1
    allow_host = args.host or settings.sandbox_egress_allow.replace(",", " ").split()[0]
    print(f"在网络 {network} 里起探针容器（镜像 {args.image}），代理 {settings.agent_proxy_url()}")
    outcomes = egress.run_probe(
        allow_host=allow_host, network=network, proxy=settings.agent_proxy_url(), image=args.image
    )
    print(egress.format_probe(outcomes))
    failed = [o for o in outcomes if not o.passed]
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(outcomes)} 条不通过：笼子没关严，别跑正式实验")
        return 1
    print(f"✅ {len(outcomes)}/{len(outcomes)} 条通过：Agent 容器只能经代理访问 {allow_host}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.egress", description=(__doc__ or "").split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="建 internal 网络并起代理容器")
    p_up.add_argument("--allow", help="放行的域名，逗号分隔；缺省读 .env 的 SANDBOX_EGRESS_ALLOW")
    p_up.add_argument("--upstream", help="代理自己的上游 host:port；缺省读 SANDBOX_EGRESS_UPSTREAM")
    p_up.add_argument(
        "--image", default=egress.DEFAULT_IMAGE, help="代理容器用的镜像（要有 python）"
    )
    p_up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="删掉代理容器").set_defaults(func=cmd_down)
    sub.add_parser("status", help="代理在不在、名单是什么").set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="代理的 ALLOW / DENY 记录")
    p_logs.add_argument("--tail", type=int, default=50)
    p_logs.set_defaults(func=cmd_logs)

    p_check = sub.add_parser("check", help="验收：Agent 同款网络里跑五条 curl")
    p_check.add_argument("--host", help="用来验证'能通'的域名；缺省取名单第一个")
    p_check.add_argument("--image", default=egress.DEFAULT_IMAGE, help="探针容器镜像（要有 curl）")
    p_check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SandboxError as exc:
        print(f"失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
