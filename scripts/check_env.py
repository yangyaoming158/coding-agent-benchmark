#!/usr/bin/env python3
"""开发环境自检。把踩过的坑固化成检查项，避免重复踩。

正式评测启动前也要跑这个（协议 C-27 要求工作区必须干净）。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

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


def run_cmd(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def check_database() -> None:
    """数据库能不能连上、迁移有没有跑到最新。

    只在装了后端依赖的情况下检查 —— 前端同学跑这个脚本时不该被数据库挡住。
    """
    backend = pathlib.Path(__file__).resolve().parent.parent / "backend"
    if not (backend / ".venv").is_dir():
        check("后端依赖已安装", False, "backend/.venv 不存在", "先跑 make install", warn_only=True)
        return

    # 必须在 backend 目录里跑：alembic.ini 里的 script_location 是相对路径
    code, out = run_cmd(["uv", "run", "alembic", "current"], cwd=str(backend))
    if code != 0:
        check(
            "数据库可连接",
            False,
            (out.splitlines() or ["连不上"])[-1],
            "先跑 make db-up",
            warn_only=True,
        )
        return
    revision = next((line for line in out.splitlines() if line and "INFO" not in line), "")
    check("数据库可连接", True, "可连接")
    check(
        "迁移已升到最新",
        "(head)" in revision,
        revision or "未初始化",
        "跑 make migrate",
        warn_only=True,
    )


#: 探针：用真正的 Settings 建一次 store 并写个文件。
#: 不在这个脚本里自己拼路径 —— 那样就有两处解析规则，迟早对不上。
_ARTIFACT_PROBE = (
    "from app.storage import create_artifact_store; "
    "s = create_artifact_store(); "
    "s.put('_env_check/probe.txt', b'ok', content_type='text/plain'); "
    "s.delete('_env_check/probe.txt'); "
    "print(getattr(s, 'root', '?'))"
)


def check_artifact_store() -> None:
    """制品目录能不能写。

    写不了的话，评测会一路跑到"保存 Agent 日志"那一步才炸，前面几分钟白跑。
    在这里花 0.2 秒问一次，比事后翻日志便宜。
    """
    backend = pathlib.Path(__file__).resolve().parent.parent / "backend"
    if not (backend / ".venv").is_dir():
        return  # check_database 已经提示过没装依赖了，不重复刷屏

    code, out = run_cmd(["uv", "run", "python", "-c", _ARTIFACT_PROBE], cwd=str(backend))
    check(
        "制品目录可写",
        code == 0,
        out.splitlines()[-1] if out else "?",
        "检查 ARTIFACT_LOCAL_ROOT 指向的目录权限",
        warn_only=True,
    )


#: 平台要求的最低 git 版本。
#:
#: 2.32 是硬门槛：工作区物化靠 `GIT_CONFIG_GLOBAL=/dev/null` 屏蔽开发机的全局配置
#: （见 backend/app/sandbox/git_cli.py），这个环境变量是 2.32 才有的。
#: 更低的版本不会报错，只会让 `core.autocrlf` 这类个人配置悄悄改掉物化结果。
MIN_GIT_VERSION = (2, 32)


def check_git() -> None:
    """git 命令行的版本。评测的工作区物化全靠它（协议 C-43）。"""
    if shutil.which("git") is None:
        check("git 命令存在", False, "未找到", "工作区物化依赖 git 命令行")
        return

    code, out = run_cmd(["git", "--version"])
    parts = out.split()
    version = parts[2] if code == 0 and len(parts) >= 3 else "?"
    try:
        numbers = tuple(int(x) for x in version.split(".")[:2])
    except ValueError:
        numbers = ()
    check(
        f"git ≥ {MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]}",
        numbers >= MIN_GIT_VERSION,
        version,
        "低版本不认 GIT_CONFIG_GLOBAL，工作区物化会被开发机的全局 git 配置影响",
    )


def main() -> int:
    print("=== 开发环境自检 ===\n")

    check(
        "Docker 命令存在",
        shutil.which("docker") is not None,
        shutil.which("docker") or "未找到",
        "在 WSL 里装原生 docker engine，不要用 Docker Desktop 集成，见 AGENTS.md 第 10 节",
    )

    code, out = run_cmd(["docker", "info", "--format", "{{json .}}"])
    if code == 0:
        try:
            info = json.loads(out)
        except json.JSONDecodeError:
            info = {}
        check("Docker daemon 可用（免 sudo）", True, info.get("ServerVersion", "?"))
        check(
            "cgroup v2",
            info.get("CgroupVersion") == "2",
            f"v{info.get('CgroupVersion', '?')}",
            "资源限额依赖 cgroup v2，v1 下内存限制行为不同",
        )
        root_dir = info.get("DockerRootDir", "")
        check(
            "连的是原生引擎而非 Docker Desktop",
            root_dir == "/var/lib/docker",
            root_dir or "?",
            "两套 docker 会抢 /var/run/docker.sock，导致镜像和容器'凭空消失'",
        )
        check(
            "daemon 配了代理",
            bool(info.get("HttpProxy")),
            info.get("HttpProxy") or "未配置",
            "dockerd 不继承 shell 的代理变量，要单独配 systemd drop-in",
            warn_only=True,
        )
        mirrors = info.get("RegistryConfig", {}).get("Mirrors") or []
        check(
            "配了 registry mirrors",
            bool(mirrors),
            mirrors[0] if mirrors else "未配置",
            "拉镜像会走 VPN，慢且容易断",
            warn_only=True,
        )
        cpu, mem = info.get("NCPU", 0), info.get("MemTotal", 0) / 2**30
        check(
            "资源够跑并发",
            cpu >= 8 and mem >= 8,
            f"{cpu} 核 / {mem:.1f} GiB",
            "并发数要相应下调，见 docs/plan/01-requirements.md §4.6",
            warn_only=True,
        )
    else:
        check("Docker daemon 可用", False, out.splitlines()[0] if out else "连不上")

    check_git()
    check_database()
    check_artifact_store()

    code, out = run_cmd(["git", "status", "--porcelain"])
    check(
        "git 工作区干净",
        code == 0 and not out,
        "干净" if not out else f"{len(out.splitlines())} 个文件有改动",
        "正式评测要求工作区干净，否则实验记录里的代码版本号无法唯一对应一份代码（协议 C-27）",
        warn_only=True,
    )

    print()
    if failure_count:
        print(f"{FAIL} {failure_count} 项必须修复")
    else:
        print(f"{OK} 全部通过（⚠️ 项不阻塞，但建议处理）")
    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
