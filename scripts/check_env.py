#!/usr/bin/env python3
"""开发环境自检。把踩过的坑固化成检查项，避免重复踩。

正式评测启动前也要跑这个（协议 C-27 要求工作区必须干净）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
failure_count = 0


def check(title: str, passed: bool, actual: str, hint: str = "", warn_only: bool = False) -> None:
    global failure_count
    if passed:
        mark = OK
    elif warn_only:
        mark = WARN
    else:
        mark = FAIL
        failure_count += 1
    print(f"{mark} {title}: {actual}")
    if not passed and hint:
        print(f"     → {hint}")


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def main() -> int:
    print("=== 开发环境自检 ===\n")

    check("Docker 命令存在", shutil.which("docker") is not None, shutil.which("docker") or "未找到",
        "在 WSL 里装原生 docker engine，不要用 Docker Desktop 集成，见 AGENTS.md 第 10 节")

    code, out = run_cmd(["docker", "info", "--format", "{{json .}}"])
    if code == 0:
        try:
            info = json.loads(out)
        except json.JSONDecodeError:
            info = {}
        check("Docker daemon 可用（免 sudo）", True, info.get("ServerVersion", "?"))
        check("cgroup v2", info.get("CgroupVersion") == "2", f"v{info.get('CgroupVersion', '?')}",
            "资源限额依赖 cgroup v2，v1 下内存限制行为不同")
        root_dir = info.get("DockerRootDir", "")
        check("连的是原生引擎而非 Docker Desktop", root_dir == "/var/lib/docker", root_dir or "?",
            "两套 docker 会抢 /var/run/docker.sock，导致镜像和容器'凭空消失'")
        check("daemon 配了代理", bool(info.get("HttpProxy")), info.get("HttpProxy") or "未配置",
            "dockerd 不继承 shell 的代理变量，要单独配 systemd drop-in", warn_only=True)
        mirrors = info.get("RegistryConfig", {}).get("Mirrors") or []
        check("配了 registry mirrors", bool(mirrors), mirrors[0] if mirrors else "未配置",
            "拉镜像会走 VPN，慢且容易断", warn_only=True)
        cpu, mem = info.get("NCPU", 0), info.get("MemTotal", 0) / 2**30
        check("资源够跑并发", cpu >= 8 and mem >= 8, f"{cpu} 核 / {mem:.1f} GiB",
            "并发数要相应下调，见 docs/plan/01-requirements.md §4.6", warn_only=True)
    else:
        check("Docker daemon 可用", False, out.splitlines()[0] if out else "连不上")

    code, out = run_cmd(["git", "status", "--porcelain"])
    check("git 工作区干净", code == 0 and not out, "干净" if not out else f"{len(out.splitlines())} 个文件有改动",
        "正式评测要求工作区干净，否则实验记录里的代码版本号无法唯一对应一份代码（协议 C-27）",
        warn_only=True)

    print()
    if failure_count:
        print(f"{FAIL} {failure_count} 项必须修复")
    else:
        print(f"{OK} 全部通过（⚠️ 项不阻塞，但建议处理）")
    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
