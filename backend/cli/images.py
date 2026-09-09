"""镜像分层构建器的命令行（E2-T3）。

    python -m cli.images build                       # 建 bench-base + 所有配方
    python -m cli.images build --env sqlfluff__py311__v1
    python -m cli.images build --env X --agent aider # 顺带叠第三层
    python -m cli.images build --force               # 忽略"已是最新"，重新建
    python -m cli.images build --no-cache            # 连 docker 的层缓存也不要
    python -m cli.images list                        # 现有镜像 + digest + 大小
    python -m cli.images gc                          # 默认只列，加 --yes 才真删

`make images-envs` 是 `build` 的快捷方式，`make images-base` 只建第一层。
（`make images` 建的是另一个东西：Golden 题那个手写镜像 `bench-golden:py311`，
它是 `environment_specs.image_tag` 还没填时的兜底，没有 bench 标签，gc 碰不到。）

## 分工

真正的构建逻辑在 `app/sandbox/images.py`，那边不碰数据库也不碰制品存储。
这个文件负责三件"和外部世界打交道"的事：

1. 把构建日志、依赖锁、配方存成制品（`envs/{environment_id}/builds/{stamp}/`）
2. 把 tag / digest / 构建状态回填进 `environment_specs`
3. 开建之前查磁盘水位

第 2 步是**尽力而为**：`environment_specs` 里没有对应行时只建镜像、不写库。
真实仓库的环境规格要等 E1-T4 挖掘器跑出来才有，而镜像得先建好 ——
题目验证流水线的 S3 拿不到镜像就一律判 `ENV_UNBUILDABLE`。构建器不该被这个顺序卡住。

## 为什么建镜像不走作业队列

`JobType.BUILD_IMAGE` 这个枚举早就有了，但这里不用它。建镜像是**实验开始之前**
做一次的事（ADR-008 说的"可在实验前夜完成"），跑在谁的机器上、什么时候跑，
都是人决定的；塞进队列只会多一条没人走的代码路径。真需要远程预热时再接。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import ArtifactKind, ArtifactOwnerType, ImageBuildStatus
from app.infrastructure.config import REPO_ROOT, Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.benchmark import EnvironmentSpec
from app.sandbox.container import SandboxError, inspect_image
from app.sandbox.git_cli import GitError
from app.sandbox.images import (
    BASE_TAG,
    BuildOutcome,
    DiskSpaceError,
    EnvRecipe,
    ImageBuildError,
    RecipeError,
    SmokeCheckError,
    agent_image_tag,
    build_agent_image,
    build_base_image,
    build_env_image,
    disk_headroom,
    docker_root_dir,
    ensure_snapshot_repo,
    gc_candidates,
    load_recipes,
    materialize_snapshot,
    remove_image,
    require_disk,
)
from app.sandbox.mirror import MirrorError
from app.sandbox.workspace import WorkspaceError
from app.storage import create_artifact_store
from app.storage.base import ArtifactRef, ArtifactStore

#: 手写的那一层在哪。
BASE_CONTEXT = REPO_ROOT / "images" / "base"
#: env 配方目录。
RECIPES_DIR = REPO_ROOT / "images" / "envs"
#: `gc` 最多剥几轮悬空镜像。每次构建留下一条中间层的链，删掉最外层会让上一层
#: 变成新的悬空镜像，一轮只能剥一层。给个上限是为了不在异常情况下空转。
MAX_GC_ROUNDS = 20

#: 第三层复用的 Agent Dockerfile 目录名 → `images/<目录>/Dockerfile`。
AGENT_CONTEXTS = {"aider": "aider", "claude-code": "claude-code"}


# ══════════════════════════════════════════════════════════════
# 制品与数据库
# ══════════════════════════════════════════════════════════════


def stamp() -> str:
    """制品 key 里的时间戳。

    用 `20260908T142514Z` 这种紧凑写法，**不能用 ISO 8601**：
    `storage.base.validate_key()` 只放行 `[A-Za-z0-9._/-]`，ISO 里的冒号会被拒收。
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def store_build_artifacts(
    store: ArtifactStore, outcome: BuildOutcome, *, environment_id: str, when: str
) -> dict[str, ArtifactRef]:
    """把构建日志、依赖锁、自查输出存成制品，返回 {名字: 引用}。

    key 形如 `envs/{environment_id}/builds/{stamp}/build.log`。

    §17.2 那张表写的是 `envs/{environment_id}/build.log.gz`，这里**多了一级时间戳**。
    理由是调环境镜像时最常做的事就是对比"上次能装、这次装不上"两份日志和两份依赖锁，
    覆盖掉就没得比了。`build_log_uri` 指向最新那一次。
    """
    prefix = f"envs/{environment_id}/builds/{when}"
    refs: dict[str, ArtifactRef] = {}

    if outcome.log:
        refs["build.log"] = store.put(
            f"{prefix}/build.log", outcome.log.encode("utf-8"), content_type="text/plain"
        )
    if outcome.lock:
        refs["requirements.lock"] = store.put(
            f"{prefix}/requirements.lock",
            outcome.lock.encode("utf-8"),
            content_type="text/plain",
        )
    summary = {
        "tag": outcome.tag,
        "layer": outcome.layer,
        "image_id": outcome.image_id,
        "digest": outcome.digest,
        "image_ref": outcome.image_ref,
        "recipe_hash": outcome.recipe_hash,
        "skipped": outcome.skipped,
        "cache_hits": outcome.cache_hits,
        "steps": outcome.steps,
        "duration_s": round(outcome.duration_s, 2),
        "pytest_version": outcome.pytest_version,
        "log_truncated": outcome.log_truncated,
        "smoke": None
        if outcome.smoke is None
        else {
            "passed": outcome.smoke.passed,
            "import_exit_code": outcome.smoke.import_exit_code,
            "import_output": outcome.smoke.import_output,
            "collect_exit_code": outcome.smoke.collect_exit_code,
            "collected": outcome.smoke.collected,
            "problems": list(outcome.smoke.problems),
        },
    }
    refs["build.json"] = store.put(
        f"{prefix}/build.json",
        json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json",
    )
    return refs


def mark_building(session: Session, environment_id: str) -> bool:
    """开建之前把状态推到 `BUILDING`。表里没这一行就返回 False。

    进程中途被杀的话，环境会停在 `BUILDING` —— 这是有意的：它看得出来
    "建到一半没结果"，比停在 `PENDING` 装作什么都没发生好。
    """
    result = session.execute(
        sa.update(EnvironmentSpec)
        .where(EnvironmentSpec.environment_id == environment_id)
        .values(build_status=ImageBuildStatus.BUILDING)
    )
    return bool(getattr(result, "rowcount", 0))


def write_back(
    session: Session,
    outcome: BuildOutcome,
    *,
    environment_id: str,
    log_ref: ArtifactRef | None,
) -> bool:
    """把构建结果写回 `environment_specs`。表里没这一行就返回 False。

    协议 C-36：引用镜像用 digest 不用 tag。tag 会被覆盖（同一个
    `bench-env:xxx` 今天和下周可能是两个镜像），digest 不会。
    """
    row = session.execute(
        sa.select(EnvironmentSpec).where(EnvironmentSpec.environment_id == environment_id)
    ).scalar_one_or_none()
    if row is None:
        return False

    had_log = bool(row.build_log_uri)
    row.image_tag = outcome.tag
    row.image_digest = outcome.digest
    row.build_status = ImageBuildStatus.READY
    row.built_at = datetime.now(UTC)
    if log_ref is None and not had_log:
        # 跳过的那次没有新日志，而库里也还没有旧的（典型场景：库刚被重建过，
        # 而镜像还在本地）。如实说一声，别让人以为制品丢了
        print("      提示     = 这次跳过了构建，所以没有新日志；要补日志跑一次 --force")
    if log_ref is not None:
        row.build_log_uri = log_ref.uri
        session.add(
            Artifact(
                owner_type=ArtifactOwnerType.ENVIRONMENT,
                owner_id=row.id,
                kind=ArtifactKind.BUILD_LOG,
                uri=log_ref.uri,
                backend=log_ref.backend,
                content_type=log_ref.content_type,
                size_bytes=log_ref.size_bytes,
                sha256=log_ref.sha256,
                compressed=log_ref.compressed,
            )
        )
    return True


def mark_failed(session: Session, environment_id: str) -> None:
    session.execute(
        sa.update(EnvironmentSpec)
        .where(EnvironmentSpec.environment_id == environment_id)
        .values(build_status=ImageBuildStatus.FAILED)
    )


# ══════════════════════════════════════════════════════════════
# build
# ══════════════════════════════════════════════════════════════


@dataclass
class _Report:
    """一次 `build` 跑完的汇总，用来决定退出码。"""

    built: list[str]
    skipped: list[str]
    failed: list[tuple[str, str]]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def _print_outcome(outcome: BuildOutcome) -> None:
    if outcome.skipped:
        print(f"  ○ {outcome.tag}  已是最新（配方哈希 {outcome.recipe_hash[:12]}），跳过")
        return
    print(
        f"  ✓ {outcome.tag}  {outcome.duration_s:.1f}s  "
        f"{outcome.steps} 步 / {outcome.cache_hits} 步命中缓存"
    )
    print(f"      image_id = {outcome.image_id}")
    print(f"      digest   = {outcome.digest or '（没有 RepoDigest）'}")
    if outcome.pytest_version:
        print(f"      pytest   = {outcome.pytest_version}")
    if outcome.smoke is not None:
        for line in outcome.smoke.import_output.splitlines():
            print(f"      自查 {line}")
        if outcome.smoke.collected is not None:
            print(f"      自查 收集到 {outcome.smoke.collected} 条用例")


def cmd_build(args: argparse.Namespace) -> int:
    settings = get_settings()
    report = _Report(built=[], skipped=[], failed=[])

    # ── 磁盘水位。开建之前查，理由见 images.require_disk() ──
    headroom = disk_headroom(docker_root_dir())
    print(f"磁盘：{headroom.describe()}，阈值 {settings.image_disk_min_free_ratio:.0%}")
    try:
        require_disk(headroom, min_free_ratio=settings.image_disk_min_free_ratio)
    except DiskSpaceError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 2

    recipes = [] if args.base_only else _select_recipes(args)
    store = create_artifact_store(settings)

    # ── 第一层 ──
    #
    # `base_ref` 是底座的 digest，要进每个 env 的配方哈希：底座换了（比如 bench-base
    # 里 pip 升了一版），上面装出来的依赖可能整个不一样，env 镜像必须跟着重建。
    #
    # `--skip-base` 时也要去查一次，**不能就这么留空** —— 留空的话配方哈希用的是
    # 一个固定的 tag 字符串，于是"底座变了"这件事对缓存完全不可见，
    # 建出来的 env 镜像会安静地停在旧底座上。
    base_ref: str | None = None
    if args.skip_base:
        try:
            base_ref = _base_digest()
        except SandboxError as exc:
            print(f"  ✗ 查不到 {BASE_TAG}：{exc}", file=sys.stderr)
            return 1
    if not args.skip_base:
        print(f"\n[1/3] {BASE_TAG}")
        try:
            outcome = build_base_image(
                context=BASE_CONTEXT, nocache=args.no_cache, force=args.force
            )
        except (ImageBuildError, OSError) as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            return 1
        _print_outcome(outcome)
        base_ref = outcome.digest or outcome.image_id
        (report.skipped if outcome.skipped else report.built).append(BASE_TAG)

    if not recipes:
        print("\n没有要建的环境镜像。")
        return report.exit_code

    # ── 第二、三层 ──
    factory = None if args.no_db else create_session_factory(create_db_engine())

    print(f"\n[2/3] 环境镜像（{len(recipes)} 个）")
    for recipe in recipes:
        try:
            outcome = _build_one_env(
                recipe,
                args=args,
                settings=settings,
                store=store,
                factory=factory,
                base_ref=base_ref,
            )
        # WorkspaceError / MirrorError / GitError 也要收：物化快照会抛它们
        # （比如仓库带 git 子模块，`git archive` 导不出来）。那是**这个配方**的问题，
        # 不该把排在后面的几个环境也带崩 —— 第一次跑真实仓库就栽在这儿：
        # pymilvus 的子模块让整轮在第 5 个配方上中止，后面三个一个都没建
        except (SandboxError, WorkspaceError, MirrorError, GitError, OSError) as exc:
            print(f"  ✗ {recipe.image_tag}：{exc}", file=sys.stderr)
            report.failed.append((recipe.image_tag, str(exc)))
            if factory is not None:
                with session_scope(factory) as session:
                    mark_failed(session, recipe.environment_id)
            continue
        (report.skipped if outcome.skipped else report.built).append(outcome.tag)

        for agent in args.agent:
            try:
                agent_outcome = build_agent_image(
                    context=REPO_ROOT / "images" / AGENT_CONTEXTS[agent],
                    environment_id=recipe.environment_id,
                    agent=agent,
                    base_tag=outcome.tag,
                    base_ref=outcome.digest or outcome.image_id,
                    nocache=args.no_cache,
                    force=args.force,
                )
            except (ImageBuildError, RecipeError, OSError) as exc:
                tag = agent_image_tag(recipe.environment_id, agent)
                print(f"  ✗ {tag}：{exc}", file=sys.stderr)
                report.failed.append((tag, str(exc)))
                continue
            _print_outcome(agent_outcome)
            (report.skipped if agent_outcome.skipped else report.built).append(agent_outcome.tag)

    print(
        f"\n[3/3] 建了 {len(report.built)} 个，跳过 {len(report.skipped)} 个，"
        f"失败 {len(report.failed)} 个"
    )
    for tag, problem in report.failed:
        print(f"  ✗ {tag}：{problem.splitlines()[0]}")
    return report.exit_code


def _build_one_env(
    recipe: EnvRecipe,
    *,
    args: argparse.Namespace,
    settings: Settings,
    store: ArtifactStore,
    factory: sessionmaker[Session] | None,
    base_ref: str | None,
) -> BuildOutcome:
    """建一个环境镜像，并把结果落制品、写库。"""
    print(f"\n  → {recipe.image_tag}（{recipe.repo_name} @ {recipe.snapshot_commit[:12]}）")

    mirror = ensure_snapshot_repo(
        recipe,
        mirror_root=REPO_ROOT / settings.mirror_root,
        snapshot_root=REPO_ROOT / settings.build_snapshot_root,
        timeout_s=settings.git_timeout_s,
        allow_fetch=not args.offline,
    )

    if factory is not None:
        with session_scope(factory) as session:
            mark_building(session, recipe.environment_id)

    # 构建上下文和快照都放临时目录：它们加起来可能上百 MB，留在仓库里迟早被误提交
    with tempfile.TemporaryDirectory(prefix="bench-img-") as tmp:
        tmp_path = Path(tmp)
        snapshot = materialize_snapshot(
            recipe,
            mirror_path=mirror,
            dest=tmp_path / "snapshot",
            timeout_s=settings.git_timeout_s,
        )
        print(f"    快照 {snapshot.file_count} 个文件，树 {snapshot.tree_sha[:12]}")
        try:
            outcome = build_env_image(
                recipe,
                context=tmp_path / "context",
                snapshot=snapshot,
                base_ref=base_ref,
                nocache=args.no_cache,
                force=args.force,
                skip_smoke=args.no_smoke,
            )
        except SmokeCheckError as exc:
            # 自查失败时**照样把制品落下来**。这是最需要看构建日志和依赖锁的时候：
            # "到底装了什么，才让 import 落在了镜像里那份快照上"。
            when = stamp()
            store_build_artifacts(
                store, exc.outcome, environment_id=recipe.environment_id, when=when
            )
            print(f"      制品     = envs/{recipe.environment_id}/builds/{when}/（自查失败）")
            raise

    _print_outcome(outcome)

    # 跳过的那次不落制品：没有构建日志、没有新的依赖锁，只会在制品目录里堆一串
    # 空目录，把真正有内容的那几次淹掉。数据库还是要写 —— 表里的行可能是这次构建
    # 之后才建出来的，digest 得补上。
    refs: dict[str, ArtifactRef] = {}
    if not outcome.skipped:
        when = stamp()
        refs = store_build_artifacts(
            store, outcome, environment_id=recipe.environment_id, when=when
        )
        print(f"      制品     = envs/{recipe.environment_id}/builds/{when}/（{len(refs)} 份）")

    if factory is not None:
        with session_scope(factory) as session:
            written = write_back(
                session,
                outcome,
                environment_id=recipe.environment_id,
                log_ref=refs.get("build.log"),
            )
        print(
            "      数据库   = environment_specs 已更新"
            if written
            else "      数据库   = 表里还没有这个环境，只建镜像不写库"
        )
    return outcome


def _base_digest() -> str:
    """第一层现在的 digest。查不到就抛 `SandboxError`（镜像还没建）。"""
    info = inspect_image(BASE_TAG)
    return info.digest or info.image_id


def _select_recipes(args: argparse.Namespace) -> list[EnvRecipe]:
    recipes = load_recipes(RECIPES_DIR)
    if args.env:
        wanted = set(args.env)
        recipes = [r for r in recipes if r.environment_id in wanted]
        missing = wanted - {r.environment_id for r in recipes}
        if missing:
            raise SystemExit(f"没有这些环境的配方：{sorted(missing)}（看 {RECIPES_DIR}）")
    return recipes


# ══════════════════════════════════════════════════════════════
# list / gc
# ══════════════════════════════════════════════════════════════


def cmd_list(args: argparse.Namespace) -> int:
    from app.sandbox.container import BENCH_LABEL, BENCH_LABEL_VALUE, get_docker_client
    from app.sandbox.images import (
        LABEL_AGENT,
        LABEL_ENVIRONMENT_ID,
        LABEL_LAYER,
        image_disk_usage,
    )

    client = get_docker_client()
    images = client.images.list(filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"})
    if not images:
        print("还没有构建器建出来的镜像。跑 `python -m cli.images build`。")
        print("（手工 `make images` 建的那几个没有 bench 标签，这里看不到，也不会被 gc 碰。）")
        return 0

    # 大小走 /system/df，不用 images.list() 里那个 Size —— 后者在 containerd 存储下
    # 报的是压缩后的内容大小，和磁盘上的实际占用差三四倍（见 image_disk_usage 的注释）
    disk = image_disk_usage(client=client)
    rows = []
    for image in images:
        labels = (image.attrs.get("Config") or {}).get("Labels") or {}
        usage = disk.get(str(image.id))
        rows.append(
            (
                labels.get(LABEL_LAYER) or "?",
                (image.tags or ["<无标签>"])[0],
                labels.get(LABEL_ENVIRONMENT_ID) or "",
                labels.get(LABEL_AGENT) or "",
                (usage.total_bytes if usage else 0) / 2**30,
                (usage.unique_bytes if usage else 0) / 2**30,
                str(image.id),
            )
        )
    rows.sort(key=lambda r: (r[0], r[1]))

    print(f"{'层':<6} {'镜像':<44} {'总大小':>8} {'独占':>8}  {'image_id':<14} 环境")
    for layer, tag, env, agent, total_gib, unique_gib, image_id in rows:
        suffix = f"{env}{'-' + agent if agent else ''}" if env else ""
        print(
            f"{layer:<6} {tag:<44} {total_gib:>7.2f}G {unique_gib:>7.2f}G  "
            f"{image_id[7:19]}  {suffix}"
        )
    unique_total = sum(r[5] for r in rows)
    print(
        f"\n合计 {len(rows)} 个镜像，各自独占的部分加起来 {unique_total:.2f} GiB。"
        "\n「总大小」含和别的镜像共用的层，相加会重复计算；「独占」是删掉它能腾出多少。"
        "\n注意两个都不等于这批镜像占的总磁盘 —— 共用的层（比如 bench-base）"
        "在「独占」里一次都没算进去。要总数看 `docker system df`。"
    )

    headroom = disk_headroom(docker_root_dir(client))
    print(f"磁盘：{headroom.describe()}")
    return 0


def _gc_inputs(*, use_db: bool) -> tuple[list[str], list[str]]:
    """回收要的两份输入：活着的环境 id，和不许删的 digest。

    **活名单** = 配方目录里有的 ∪ `environment_specs` 表里有的。两边都要看：
    配方是"我们打算要的"，表是"已经有题目在用的"。只看配方，会把一个已经被题目
    引用、但配方文件被误删的环境镜像删掉。

    **不许删的 digest** = `environment_specs.image_digest` 整列。协议 C-36 要求
    运行记录按 digest 引用镜像，删掉一个还被记着的 digest，等于把那次实验的
    可复现性抹掉。`--no-db` 时这一列拿不到，所以那种模式下只删"环境已经不存在"的，
    不碰任何没有 tag 的旧版本。
    """
    live = {r.environment_id for r in load_recipes(RECIPES_DIR)}
    if not use_db:
        return sorted(live), []
    factory = create_session_factory(create_db_engine())
    with session_scope(factory) as session:
        rows = session.execute(
            sa.select(EnvironmentSpec.environment_id, EnvironmentSpec.image_digest)
        ).all()
    live |= {row[0] for row in rows}
    return sorted(live), [row[1] for row in rows if row[1]]


def cmd_gc(args: argparse.Namespace) -> int:
    live, keep_digests = _gc_inputs(use_db=not args.no_db)
    print(f"活着的环境：{len(live)} 个 —— {', '.join(live) or '（一个都没有）'}")
    if args.no_db:
        print("（--no-db：拿不到库里记着的 digest，所以不动任何没有 tag 的旧版本）")
    else:
        print(f"库里记着 {len(keep_digests)} 个 digest，这些一律不删（协议 C-36）")
    print()

    candidates = gc_candidates(live_environment_ids=live, keep_digests=keep_digests)
    if not candidates:
        print("没有可以回收的镜像。")
        return 0

    total = sum(c.size_bytes for c in candidates) / 2**30
    for candidate in candidates:
        print(f"  {candidate.display}")
    print(f"\n合计 {len(candidates)} 个，删掉能腾出约 {total:.2f} GiB")

    if not args.yes:
        print("\n这是 --dry-run（默认）。真要删加 --yes。")
        return 0

    # 删完要再查一遍：删掉一个镜像会让它的上一层**变成**悬空镜像（每次构建都会留下
    # 一条中间层的链），一轮只能剥掉最外面那一层。所以循环到没有新的为止。
    #
    # 被删掉的都是**旧构建**分叉之后的那些层。和现役镜像共用的层 docker 自己会保住
    # （层是引用计数的），所以这一步不会让下次重建从零开始 —— 只有"改回上一版配方"
    # 那种情况才会重新付一次构建代价。
    failures: list[str] = []
    removed = 0
    reclaimed = 0
    for _round in range(MAX_GC_ROUNDS):
        if not candidates:
            break
        for candidate in candidates:
            problem = remove_image(candidate)
            if problem:
                failures.append(problem)
                print(f"  ✗ {problem}")
                continue
            removed += 1
            reclaimed += candidate.size_bytes
            name = candidate.tags[0] if candidate.tags else candidate.image_id[7:19]
            print(f"  ✓ 删掉 {name}")
        candidates = [
            c
            for c in gc_candidates(live_environment_ids=live, keep_digests=keep_digests)
            if c.image_id not in {f.split("：")[0] for f in failures}
        ]

    print(f"\n删掉 {removed} 个，约 {reclaimed / 2**30:.2f} GiB")
    headroom = disk_headroom(docker_root_dir())
    print(f"磁盘：{headroom.describe()}")
    return 1 if failures else 0


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.images", description="三层镜像的构建与回收（E2-T3）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="建 bench-base / bench-env / bench-agent")
    build.add_argument("--env", action="append", default=[], help="只建这些环境，可重复")
    build.add_argument(
        "--agent",
        action="append",
        default=[],
        choices=sorted(AGENT_CONTEXTS),
        help="顺带在每个环境上叠一层 Agent 镜像，可重复",
    )
    build.add_argument("--skip-base", action="store_true", help="不建第一层")
    build.add_argument("--base-only", action="store_true", help="只建第一层")
    build.add_argument(
        "--force", action="store_true", help="配方哈希没变也重建（docker 层缓存仍然生效）"
    )
    build.add_argument("--no-cache", action="store_true", help="连 docker 的层缓存也不用")
    build.add_argument(
        "--no-smoke",
        action="store_true",
        help="跳过建完自查。**只在调试时用** —— 自查挡的是补丁不生效这类不报错的故障",
    )
    build.add_argument("--offline", action="store_true", help="本地没有快照就直接失败，不联网拉")
    build.add_argument("--no-db", action="store_true", help="不连数据库，只建镜像")
    build.set_defaults(func=cmd_build)

    listing = sub.add_parser("list", help="列出构建器建出来的镜像")
    listing.set_defaults(func=cmd_list)

    gc = sub.add_parser("gc", help="回收无引用的镜像（默认只列不删）")
    gc.add_argument("--yes", action="store_true", help="真的删")
    gc.add_argument("--no-db", action="store_true", help="不连数据库，只按配方目录判断")
    gc.set_defaults(func=cmd_gc)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if shutil.which("docker") is None:
        print("找不到 docker 命令。见 AGENTS.md 第 10 节。", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    func = args.func
    assert callable(func)
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
