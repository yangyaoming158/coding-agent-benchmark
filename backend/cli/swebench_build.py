"""按官方配方在本机建 SWE-bench 镜像（E1-T7，`cli.swebench build` 的实现）。

    python -m cli.swebench build                 # base → 20 个 env → 50 个 instance，已有的跳过
    python -m cli.swebench build --only pallets__flask-5014
    python -m cli.swebench build --jobs 2        # env / instance 两个一起建（编译重的仓库别开太多）

三层镜像和官方一样：base（ubuntu + miniconda）→ env（conda 环境 + 钉死版本的 pip 包）→
instance（仓库代码 + `pip install -e .`）。配方原文在 `datasets/swebench/build-specs.json`，
改写规则在 `app.benchmark.swebench_recipes`（只改源和 clone 方式），这里只管：
写构建上下文、从本地 git 镜像导出仓库 tar、调 `docker build`、探测、登记进 `images.json`。

建出来的镜像走和官方镜像**同一条**后续流水线：`images.json` 里记 `source: local-build`，
`cli.swebench import` 据此绑 `built_environment()`，测试命令、`pre_test_command` 都不变。

## 代理

`docker build` 里的下载分两类：清华源（apt / conda / pip）直连，其余（几乎没有）走代理。
所以把代理作为 build-arg 传进去，同时把清华的两个主机放进 `NO_PROXY` —— 不放的话
清华的流量也会绕出境那条线，那就白改源了。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.benchmark.swebench_import import VerifiedInstance, official_environment_id
from app.benchmark.swebench_recipes import (
    BASE_TAG,
    MIRROR_HOSTS,
    QHULL_TARBALL,
    QHULL_URL,
    BuildSpecs,
    base_context_files,
    build_context_files,
    env_context_files,
    env_tag,
    instance_tag,
    load_build_specs,
    needs_qhull,
)
from app.infrastructure.config import REPO_ROOT, get_settings
from app.sandbox.container import ImageNotFoundError, get_docker_client, inspect_image
from app.sandbox.git_cli import GitError
from app.sandbox.mirror import MirrorManager

SPECS_FILE = REPO_ROOT / "datasets" / "swebench" / "build-specs.json"
BUILD_ROOT = REPO_ROOT / "var" / "cache" / "swebench" / "build"
LOG_ROOT = REPO_ROOT / "var" / "swebench-logs" / "build"
QHULL_CACHE = REPO_ROOT / "var" / "cache" / "swebench" / QHULL_TARBALL

#: 一次 `docker build` 最多等多久。matplotlib 要装 texlive 再编译，半小时打底。
BUILD_TIMEOUT_S = 3 * 3600


class BuildError(RuntimeError):
    """一个镜像没建成。附日志路径，别让人去翻 stdout。"""


def _log_path(tag: str) -> Path:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return LOG_ROOT / (re.sub(r"[^A-Za-z0-9_.-]+", "_", tag) + ".log")


def _proxy_build_args(proxy: str | None) -> list[str]:
    """代理进 build-arg，清华源进 NO_PROXY（大小写各一份，工具认的不一样）。"""
    if not proxy:
        return []
    no_proxy = ",".join(("localhost", "127.0.0.1", "::1", *MIRROR_HOSTS))
    args: list[str] = []
    for name, value in (
        ("HTTP_PROXY", proxy),
        ("HTTPS_PROXY", proxy),
        ("NO_PROXY", no_proxy),
        ("http_proxy", proxy),
        ("https_proxy", proxy),
        ("no_proxy", no_proxy),
    ):
        args += ["--build-arg", f"{name}={value}"]
    return args


def image_exists(tag: str) -> bool:
    try:
        inspect_image(tag, client=get_docker_client())
    except ImageNotFoundError:
        return False
    return True


def docker_build(
    context: Path, tag: str, *, proxy: str | None, timeout_s: int = BUILD_TIMEOUT_S
) -> Path:
    """跑 `docker build`，输出全落日志文件。失败抛 `BuildError`，带日志尾巴。"""
    log = _log_path(tag)
    command = [
        "docker",
        "build",
        "--progress=plain",
        *_proxy_build_args(proxy),
        "-t",
        tag,
        str(context),
    ]
    started = time.monotonic()
    with log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        handle.flush()
        try:
            completed = subprocess.run(
                command, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise BuildError(f"{tag} 建了 {timeout_s} 秒还没完，日志 {log}") from exc
    seconds = time.monotonic() - started
    if completed.returncode != 0:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
        raise BuildError(
            f"{tag} 构建失败（退出码 {completed.returncode}，{seconds:.0f} 秒），日志 {log}\n{tail}"
        )
    return log


def _write_context(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")


def ensure_qhull() -> Path:
    """matplotlib 要的 qhull 源码包，下一次缓存在 var/cache/。1 MB，走代理也无所谓。"""
    if QHULL_CACHE.is_file() and QHULL_CACHE.stat().st_size > 100_000:
        return QHULL_CACHE
    from cli.swebench import make_client  # 复用带代理的 httpx 客户端

    with make_client(timeout_s=120.0) as client:
        response = client.get(QHULL_URL)
        response.raise_for_status()
    QHULL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    QHULL_CACHE.write_bytes(response.content)
    return QHULL_CACHE


def export_repo_tar(mirrors: MirrorManager, instance: VerifiedInstance, dest: Path) -> None:
    """从本地 git 镜像导出 base_commit 的文件树（纯文件，不带 .git）。"""
    if not mirrors.has_commit(instance.repo, instance.base_commit):
        raise BuildError(
            f"{instance.instance_id}：本地 git 镜像里没有 {instance.base_commit[:12]}，"
            "先 `make swebench-mirror`"
        )
    with dest.open("wb") as handle:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(mirrors.path_for(instance.repo)),
                "archive",
                "--format=tar",
                instance.base_commit,
            ],
            stdout=handle,
            stderr=subprocess.PIPE,
            timeout=600,
            check=False,
        )
    if completed.returncode != 0:
        raise BuildError(f"git archive 失败：{completed.stderr.decode(errors='replace')[-200:]}")


# ══════════════════════════════════════════════════════════════
# 三层
# ══════════════════════════════════════════════════════════════


def build_base(specs: BuildSpecs, *, proxy: str | None, force: bool) -> str:
    if image_exists(BASE_TAG) and not force:
        return "已有"
    context = BUILD_ROOT / "base"
    _write_context(context, base_context_files(specs))
    started = time.monotonic()
    docker_build(context, BASE_TAG, proxy=proxy)
    return f"建好，{time.monotonic() - started:.0f} 秒"


def build_env(specs: BuildSpecs, env_key: str, *, proxy: str | None, force: bool) -> str:
    tag = env_tag(env_key)
    if image_exists(tag) and not force:
        return "已有"
    context = BUILD_ROOT / "env" / tag.rsplit(":", 1)[-1]
    _write_context(context, env_context_files(specs, env_key))
    started = time.monotonic()
    docker_build(context, tag, proxy=proxy)
    return f"建好，{time.monotonic() - started:.0f} 秒"


def build_instance(
    specs: BuildSpecs,
    instance: VerifiedInstance,
    mirrors: MirrorManager,
    *,
    proxy: str | None,
    force: bool,
) -> str:
    tag = instance_tag(instance.instance_id)
    if image_exists(tag) and not force:
        return "已有"
    context = BUILD_ROOT / "instance" / instance.instance_id
    _write_context(
        context, build_context_files(specs, instance.instance_id, version=instance.version or "0")
    )
    export_repo_tar(mirrors, instance, context / "repo.tar")
    if needs_qhull(specs.instances[instance.instance_id]["install_repo_script"]):
        (context / QHULL_TARBALL).write_bytes(ensure_qhull().read_bytes())
    started = time.monotonic()
    try:
        docker_build(context, tag, proxy=proxy)
    finally:
        (context / "repo.tar").unlink(missing_ok=True)  # matplotlib 的 tar 上百 MB，别留
    return f"建好，{time.monotonic() - started:.0f} 秒"


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def _run_parallel(
    jobs: int, items: Sequence[Any], worker: Any
) -> list[tuple[Any, str | None, str | None]]:
    """并行跑，返回 `(item, 结果, 错误)`。一个失败不影响别的。"""
    results: list[tuple[Any, str | None, str | None]] = []

    def guarded(item: Any) -> tuple[Any, str | None, str | None]:
        try:
            return item, worker(item), None
        except Exception as exc:  # 建镜像的任何失败都要逐个报，不能整批崩
            return item, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for item, outcome, error in pool.map(guarded, items):
            results.append((item, outcome, error))
            label = outcome if outcome else f"✗ {error}"
            print(f"  {_label(item):<44} {label}")
    return results


def _label(item: Any) -> str:
    return item.instance_id if isinstance(item, VerifiedInstance) else env_tag(str(item))


def build_all(
    chosen: Iterable[VerifiedInstance],
    *,
    jobs: int,
    force: bool,
    skip_base: bool,
) -> tuple[list[VerifiedInstance], list[tuple[str, str]]]:
    """三层顺着建。返回（建好的题, [(题或环境, 错误)]）。"""
    from cli.swebench import http_proxy_from_env

    specs = load_build_specs(SPECS_FILE)
    settings = get_settings()
    mirrors = MirrorManager(Path(settings.mirror_root), timeout_s=settings.git_timeout_s)
    proxy = http_proxy_from_env()
    chosen = list(chosen)
    failures: list[tuple[str, str]] = []

    print(f"配方：swebench {specs.swebench_version}，{len(chosen)} 道题\n")
    if not skip_base:
        print(f"base {BASE_TAG}")
        try:
            print(f"  {build_base(specs, proxy=proxy, force=force)}")
        except (BuildError, GitError) as exc:
            print(f"  ✗ {exc}")
            return [], [("base", str(exc))]

    env_keys = sorted({specs.instances[i.instance_id]["env_image_key"] for i in chosen})
    print(f"\nenv 镜像 {len(env_keys)} 个（并行 {jobs}）")
    env_results = _run_parallel(
        jobs, env_keys, lambda key: build_env(specs, key, proxy=proxy, force=force)
    )
    broken_envs = {key for key, _, error in env_results if error}
    failures += [(env_tag(key), error) for key, _, error in env_results if error]

    buildable = [
        i for i in chosen if specs.instances[i.instance_id]["env_image_key"] not in broken_envs
    ]
    print(f"\ninstance 镜像 {len(buildable)} 个（并行 {jobs}）")
    inst_results = _run_parallel(
        jobs, buildable, lambda inst: build_instance(specs, inst, mirrors, proxy=proxy, force=force)
    )
    built = [inst for inst, _, error in inst_results if not error]
    failures += [(inst.instance_id, error) for inst, _, error in inst_results if error]
    return built, failures


def cmd_build(args: argparse.Namespace) -> int:
    from cli.swebench import (
        IMAGES_FILE,
        _now_iso,
        _read_json,
        _sample_and_instances,
        _write_json,
        probe_image,
    )

    _, chosen = _sample_and_instances(args)
    if args.only:
        wanted = set(args.only)
        chosen = [instance for instance in chosen if instance.instance_id in wanted]
    status = _read_json(IMAGES_FILE)
    if not args.force:
        # 官方镜像已经拉到的不用建；本机建过的也不用重来
        chosen = [i for i in chosen if (status.get(i.instance_id) or {}).get("status") != "pulled"]
    if args.limit is not None:
        chosen = chosen[: args.limit]
    if not chosen:
        print("没有要建的题（都拉到或建好了）")
        return 0
    if not SPECS_FILE.exists():
        print(f"没有 {SPECS_FILE}，先跑 scripts/export_swebench_specs.py")
        return 1

    built, failures = build_all(chosen, jobs=args.jobs, force=args.force, skip_base=args.skip_base)

    client = get_docker_client()
    print("\n探测 + 登记")
    for instance in built:
        tag = instance_tag(instance.instance_id)
        try:
            info = inspect_image(tag, client=client)
            probe = probe_image(client, tag)
        except Exception as exc:  # 探测失败也要记下来，别让镜像白建
            failures.append((instance.instance_id, f"探测失败：{exc}"))
            print(f"  ✗ {instance.instance_id:<36} 探测失败：{str(exc)[:100]}")
            continue
        status[instance.instance_id] = {
            "image": tag,
            "status": "pulled",
            "source": "local-build",
            "environment_id": official_environment_id(instance.instance_id),
            "digest": info.digest,
            "image_id": info.image_id,
            "python_version": probe.get("python"),
            "pytest_version": probe.get("pytest"),
            "testbed_head": probe.get("testbed_head"),
            "testbed_parent": probe.get("testbed_parent"),
            "testbed_matches_base": True,  # 本机建的 /testbed 就是 base_commit 的树，没有别的提交
            "ignored_files": probe.get("ignored_files"),
            "pulled_at": _now_iso(),
        }
        _write_json(IMAGES_FILE, status)
        versions = f"python {probe.get('python')}，pytest {probe.get('pytest')}"
        print(f"  ✓ {instance.instance_id:<36} {versions}")

    print(f"\n建好 {len(built)}，失败 {len(failures)}；状态在 {IMAGES_FILE}")
    for name, error in failures:
        print(f"  ✗ {name}: {error.splitlines()[0][:160]}")
    return 1 if failures else 0


def add_build_parser(sub: Any, add_sample_args: Any) -> None:
    parser = sub.add_parser("build", help="按官方配方本机建镜像（依赖走清华源，不看出境线路脸色）")
    add_sample_args(parser)
    parser.add_argument("--only", action="append", help="只建这几道（instance_id，可重复给）")
    parser.add_argument("--limit", type=int, default=None, help="最多建几道")
    parser.add_argument("--jobs", type=int, default=2, help="env / instance 各并行几个，默认 2")
    parser.add_argument("--force", action="store_true", help="已有的镜像也重建")
    parser.add_argument(
        "--skip-base", action="store_true", help="跳过 base（已经建好时省一次检查）"
    )
    parser.set_defaults(func=cmd_build)


__all__ = ["BuildError", "add_build_parser", "build_all", "cmd_build", "docker_build"]
