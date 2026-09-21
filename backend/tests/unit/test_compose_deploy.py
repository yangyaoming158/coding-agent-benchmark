"""docker compose 一键部署的配置检查（E10-T1 AC ④、⑨）。

这里验的是"配好了"和"真的会拦"之间的那几处：

- `.env` 进不了构建上下文。里面是各家大模型的 Key，进了镜像层 `docker history`
  就能翻出来，而构建不会有任何提示
- Worker 拿着 docker.sock 等于拿着宿主机 root，它不能发布任何端口（§10.6）；
  api 不需要 docker，就不给它挂 socket —— 少一个拿 root 的进程
- 缺 `ADMIN_TOKEN` / `BENCH_REPO_DIR` 时 compose 在起容器**之前**就报错，
  而不是 api 起来又崩、Worker 把一个空目录当工作区

前两组读的是 `docker compose config` 解析后的结果，不是正则匹配 yml 文本 ——
锚点、变量默认值、profiles 都只有 compose 自己解析得对。这几条要 docker 命令，
带 `docker` 标记；`.dockerignore` 那一条不需要，`make test` 就跑。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

#: `docker compose config` 要的最小环境：两个必填变量给假值，其余走默认
FAKE_ENV = {"BENCH_REPO_DIR": "/srv/bench", "ADMIN_TOKEN": "test-token"}


def _compose_config(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """跑一次 `docker compose config`，返回原始结果（调用方自己看 returncode）。

    环境用 `env=` 整个替换，而不是在当前环境上叠加：仓库根目录的 `.env` 是开发者自己的，
    里面本来就有 ADMIN_TOKEN，叠加的话"缺变量要报错"那条永远测不出来。
    compose 仍会读 `.env` 文件做插值 —— 所以缺变量的用例把它显式设成空串，
    空串在 `${VAR:?}` 语法下也算缺。
    """
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", **FAKE_ENV, **(extra_env or {})}
    return subprocess.run(
        ["docker", "compose", "--profile", "tools", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _services() -> dict[str, Any]:
    result = _compose_config()
    assert result.returncode == 0, result.stderr
    services: dict[str, Any] = json.loads(result.stdout)["services"]
    return services


def _mounts_docker_sock(service: dict[str, Any]) -> bool:
    return any("docker.sock" in str(v.get("source", "")) for v in service.get("volumes", []))


# ── 不需要 docker 的那一条 ─────────────────────────────────────


def test_dockerignore_keeps_secrets_and_bulk_out_of_build_context() -> None:
    """`.dockerignore` 是"先全排除再放行"的写法：`.env` 和 `var/` 不在放行名单里。

    只检查规则文本，不真的建镜像。真正的验证（用 `FROM scratch` 建一个空镜像、
    列出上下文里有什么）在 E10-T1 的实录里做过一次，这里守住的是"别人改了规则却没意识到"。
    """
    rules = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules[0] == "*", "第一条必须是排除一切，后面才是放行"
    allowed = {r[1:] for r in rules if r.startswith("!")}
    # 放行名单里不能出现这些：.env（密钥）、var（几十 GB 的运行产物）、backend 整目录（含 .venv）
    for forbidden in (".env", "var", "backend", "backend/app", "images", ".git"):
        assert forbidden not in allowed, f"{forbidden} 不该进构建上下文"
    # 放行的 backend 只能是两份依赖清单
    assert {"backend/pyproject.toml", "backend/uv.lock"} <= allowed
    # frontend 放行了，但它下面的 node_modules / .next / .env* 要再排掉
    assert "frontend" in allowed
    assert {"frontend/node_modules", "frontend/.next", "frontend/.env*"} <= set(rules)


# ── 要 docker 命令的几条 ───────────────────────────────────────

pytestmark_docker = pytest.mark.docker


@pytestmark_docker
def test_only_worker_and_cli_hold_docker_socket_and_worker_has_no_ports() -> None:
    services = _services()
    assert set(services) >= {"postgres", "migrate", "api", "worker", "frontend", "cli"}

    assert _mounts_docker_sock(services["worker"]), "Worker 起评测容器要 docker.sock（DooD）"
    assert _mounts_docker_sock(services["cli"]), "cli.validate / cli.images 要起容器"
    for name in ("api", "migrate", "frontend", "postgres"):
        assert not _mounts_docker_sock(services[name]), f"{name} 不该拿到 docker.sock"

    assert not services["worker"].get("ports"), "拿着 docker.sock 的 Worker 不能发布端口（§10.6）"
    assert not services["cli"].get("ports")
    assert services["cli"].get("profiles") == ["tools"], "cli 只给 run 用，up 不该起它"


@pytestmark_docker
def test_repo_is_mounted_at_its_host_path_in_every_backend_service() -> None:
    """DooD 的前提：容器里看到的仓库路径 = 宿主机路径，否则评测容器会挂到一个空目录。"""
    services = _services()
    for name in ("api", "worker", "migrate", "cli"):
        binds = {
            (v.get("source"), v.get("target"))
            for v in services[name].get("volumes", [])
            if "docker.sock" not in str(v.get("source", ""))
        }
        assert ("/srv/bench", "/srv/bench") in binds, f"{name} 的仓库挂载源和目标不一致"
        assert services[name]["working_dir"] == "/srv/bench/backend"
        env = services[name]["environment"]
        assert env["BENCH_REPO_DIR"] == "/srv/bench"
        assert env["BENCH_DATABASE_URL"] == "postgresql+psycopg://bench:bench@postgres:5432/bench"


@pytestmark_docker
def test_api_and_worker_wait_for_migration() -> None:
    services = _services()
    for name in ("api", "worker", "cli"):
        cond = services[name]["depends_on"]["migrate"]["condition"]
        assert cond == "service_completed_successfully", f"{name} 要等迁移跑完再起"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"


@pytestmark_docker
def test_ports_and_cors_follow_env_overrides() -> None:
    """三个端口都能改，而且 CORS 名单跟着前端端口走 —— 改了端口前端连不上后端是最常见的坑。"""
    result = _compose_config(
        {"BENCH_API_PORT": "18000", "BENCH_WEB_PORT": "13000", "BENCH_PG_PORT": "15433"}
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["api"]["ports"][0]["published"] == "18000"
    assert services["frontend"]["ports"][0]["published"] == "13000"
    assert services["postgres"]["ports"][0]["published"] == "15433"
    origins = services["api"]["environment"]["BENCH_DEV_FRONTEND_ORIGINS"]
    assert origins == "http://localhost:13000,http://127.0.0.1:13000"
    assert services["frontend"]["build"]["args"]["NEXT_PUBLIC_API_BASE"] == "http://localhost:18000"


@pytestmark_docker
@pytest.mark.parametrize(
    ("missing", "hint"),
    [("ADMIN_TOKEN", "ADMIN_TOKEN"), ("BENCH_REPO_DIR", "BENCH_REPO_DIR")],
)
def test_missing_required_variable_fails_before_any_container_starts(
    missing: str, hint: str
) -> None:
    result = _compose_config({missing: ""})
    assert result.returncode != 0, f"缺 {missing} 时 compose config 必须失败"
    assert hint in result.stderr
