"""镜像构建器里真的会起 Docker 的那一半（E2-T3 的验收标准）。

带 `docker` 标记，`make test` 和 CI 都跳过，要跑得用 `make test-docker`。

四条 AC 在这里各有对应的用例：

- 同一 env 重复构建命中缓存 → `test_rebuild_is_skipped_by_recipe_hash`
  和 `test_force_rebuild_hits_the_layer_cache`
- digest 记录 → `test_build_end_to_end`（写进 `environment_specs` 那一半在
  `cli.images` 里，靠人工验，因为这一组不挂数据库）
- 构建日志落制品 → `test_build_end_to_end` 里的日志和依赖锁
- 磁盘水位检查生效 → 纯函数，在 `tests/unit/test_images.py`

**这里最要紧的是那一对正反用例**：`test_smoke_check_accepts_correct_source_roots` 和
`test_smoke_check_catches_workspace_shadowing`。它们用**同一个仓库**建两次，只差
`workspace_source_roots` 一个字段：写对了，`import` 落在挂进来的工作区；写错了，
落在镜像里那份快照。后者必须让构建失败。

只有正向那一条的话，一个永远通过的自查也能让它全绿 —— 和 E3-T1 给契约套件配六个
坏适配器是同一个道理。
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.sandbox.container import get_docker_client
from app.sandbox.images import (
    BASE_TAG,
    LABEL_ENVIRONMENT_ID,
    LABEL_LAYER,
    LABEL_RECIPE_HASH,
    LAYER_ENV,
    EnvRecipe,
    ImageBuildError,
    SmokeCheckError,
    Snapshot,
    build_env_image,
    gc_candidates,
    remove_image,
)

pytestmark = pytest.mark.docker

#: 装依赖时关掉构建隔离。
#:
#: 两个理由：一是自查容器和构建都要能离线跑，构建隔离会去 PyPI 拉一份新的 setuptools；
#: 二是这正好走了 §8.8 坑 ⑦ 那条路径（只有 setup.py 的老项目要靠它）。
INSTALL = "python -m pip install -q --no-cache-dir --no-build-isolation -e ."

PYPROJECT = """\
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "shadowdemo"
version = "0.1.0"

[tool.setuptools.packages.find]
where = ["src"]
"""

#: 包里写一句"我是哪一份"，出问题时看得见到底 import 到了谁。
MODULE = "ORIGIN = {origin!r}\n"

TEST_FILE = """\
from shadowdemo import ORIGIN


def test_origin_is_the_workspace_copy():
    assert ORIGIN == "workspace"
"""


def write_repo(root: Path, *, origin: str) -> None:
    """写一个 **src 布局** 的极小包。

    src 布局是关键：包在 `src/shadowdemo`，工作区根底下根本没有 `shadowdemo`
    这个名字。平铺布局的仓库靠 pytest 把 rootdir 放进 `sys.path` 就碰巧对了，
    src 布局不会 —— 这正是要验的那种情况。
    """
    (root / "src" / "shadowdemo").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / "src" / "shadowdemo" / "__init__.py").write_text(
        MODULE.format(origin=origin), encoding="utf-8"
    )
    (root / "tests" / "test_origin.py").write_text(TEST_FILE, encoding="utf-8")


def make_recipe(environment_id: str, *, source_roots: tuple[str, ...]) -> EnvRecipe:
    return EnvRecipe(
        environment_id=environment_id,
        repo_name="bench-test/shadowdemo",
        repo_url="golden://bench-test/shadowdemo",
        # 这个测试不走 git，快照直接指向一个目录；commit 只是为了过配方的格式校验
        snapshot_commit="0" * 40,
        install_steps=(INSTALL,),
        workspace_source_roots=source_roots,
        import_check=("shadowdemo",),
        collect_check=True,
    )


@pytest.fixture(scope="module")
def base_image_present() -> None:
    """没有 bench-base 就跳过 —— 建它要几十秒，不该混进这一组用例里。"""
    try:
        get_docker_client().images.get(BASE_TAG)
    except Exception as exc:  # 镜像不在、连不上 daemon 都是跳过的理由
        pytest.skip(f"没有 {BASE_TAG}，先跑 `make images-base`：{exc}")


@pytest.fixture
def snapshot(tmp_path: Path) -> Snapshot:
    """镜像里那一份快照。故意写成 `origin="image"`。

    工作区那一份写 `origin="workspace"`，于是"到底 import 到了谁"是一个
    肉眼可辨的字符串，不用去比对路径。
    """
    root = tmp_path / "snapshot"
    write_repo(root, origin="image")
    return Snapshot(path=root, commit="0" * 40, tree_sha="deadbeef" * 5, file_count=4)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """评测时挂进容器的那一份。"""
    root = tmp_path / "workspace"
    write_repo(root, origin="workspace")
    return root


@pytest.fixture
def cleanup_images() -> Iterator[list[str]]:
    """用完把建出来的镜像删掉，别在开发机上越攒越多。

    **按 tag 删还不够，悬空的那些也要删**（2026-09-12 补，`05-sandbox.md` §10.10）：
    同一个 tag 建第二次时，上一版就成了没有 tag、但**还带着
    `bench.environment_id` 标签**的悬空镜像。而 `gc_candidates()` 的第 2 条判据
    （没 tag 又没被库里引用的旧构建照样回收）会把它列出来，于是
    `test_gc_finds_and_removes_a_dead_environment` 的最后一条断言永远差一个。

    更糟的是它**自我延续**：那条用例一红就停在删镜像之前，又留下一个悬空的，
    下一次照样红。清悬空的这一步就是为了断掉这个循环。
    """
    tags: list[str] = []
    yield tags
    client = get_docker_client()
    # tag 形如 `bench-env:bench-test__gcme__py311`，冒号后面那截就是 environment_id
    mine = {tag.split(":", 1)[-1] for tag in tags}
    for tag in tags:
        # 清理失败不该让用例变红：镜像可能已经被用例自己删掉了
        with contextlib.suppress(Exception):
            client.images.remove(tag, force=True)
    with contextlib.suppress(Exception):
        for image in client.images.list(filters={"dangling": True}):
            labels = (image.attrs.get("Config") or {}).get("Labels") or {}
            if labels.get("bench.environment_id") in mine:
                with contextlib.suppress(Exception):
                    client.images.remove(image.id, force=True)


# ══════════════════════════════════════════════════════════════
# 正反一对：sys.path 前插到底管不管用
# ══════════════════════════════════════════════════════════════


def test_smoke_check_accepts_correct_source_roots(
    base_image_present: None,
    snapshot: Snapshot,
    workspace: Path,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """`workspace_source_roots` 写对了，`import` 落在挂进来的工作区。"""
    recipe = make_recipe("bench-test__shadow-ok__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)

    outcome = build_env_image(recipe, context=tmp_path / "ctx-ok", snapshot=snapshot)

    assert outcome.smoke is not None
    assert outcome.smoke.passed
    assert "/workspace/src/shadowdemo/__init__.py" in outcome.smoke.import_output
    assert outcome.smoke.collected == 1


def test_smoke_check_catches_workspace_shadowing(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """`workspace_source_roots` 写错了，构建**必须失败**。

    这是整个 E2-T3 里最值钱的一条用例。没有它的话，一个源码目录写错的环境镜像
    照样建得出来、跑得起来、测试还可能全绿 —— 只是被测 AI 改的代码从来没被执行过，
    而 Oracle 哨兵会从 100% 悄悄掉下去，日志里一点异常都没有。
    """
    recipe = make_recipe("bench-test__shadow-bad__py311", source_roots=("",))
    cleanup_images.append(recipe.image_tag)

    with pytest.raises(SmokeCheckError) as exc_info:
        build_env_image(recipe, context=tmp_path / "ctx-bad", snapshot=snapshot)

    message = str(exc_info.value)
    assert "自查没过" in message
    # 报错信息要说清楚 import 到了哪儿，以及该改哪个字段
    assert "/opt/repo/src/shadowdemo" in message
    assert "workspace_source_roots" in message


def test_no_smoke_lets_a_broken_image_through(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """`--no-smoke` 确实会放行上面那个坏镜像 —— 所以它只该在调试时用。

    钉这一条是为了让"跳过自查的代价"有据可查，而不是一个听起来无害的开关。
    """
    recipe = make_recipe("bench-test__shadow-bad__py311", source_roots=("",))
    cleanup_images.append(recipe.image_tag)

    outcome = build_env_image(
        recipe, context=tmp_path / "ctx-noskip", snapshot=snapshot, skip_smoke=True
    )
    assert outcome.smoke is None
    assert outcome.digest


# ══════════════════════════════════════════════════════════════
# 缓存（AC 第一条）
# ══════════════════════════════════════════════════════════════


def test_build_end_to_end(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """一次完整构建该带回来的全部事实。"""
    recipe = make_recipe("bench-test__endtoend__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)

    outcome = build_env_image(recipe, context=tmp_path / "ctx", snapshot=snapshot)

    assert outcome.tag == "bench-env:bench-test__endtoend__py311"
    assert outcome.layer == LAYER_ENV
    assert outcome.image_id.startswith("sha256:")
    # 本机构建的镜像在 containerd 镜像存储下也有 RepoDigests（E1-T3 已确认）
    assert outcome.digest is not None
    assert outcome.image_ref.startswith("bench-env@sha256:")
    assert not outcome.skipped
    assert outcome.steps > 0

    # 构建日志：制品就是它，空的等于没有证据
    assert "Successfully built" in outcome.log or "Successfully tagged" in outcome.log

    # 依赖锁（§10.4 的 pip freeze lock）
    assert outcome.lock is not None
    assert "shadowdemo" in outcome.lock
    assert "pytest==" in outcome.lock

    # pytest 版本：仓库的 test extra 有可能把 base 层钉的版本降下去，要看得见
    assert outcome.pytest_version == "9.1.1"

    labels = (
        get_docker_client().images.get(recipe.image_tag).attrs.get("Config", {}).get("Labels", {})
    )
    assert labels[LABEL_LAYER] == LAYER_ENV
    assert labels[LABEL_ENVIRONMENT_ID] == recipe.environment_id
    assert labels[LABEL_RECIPE_HASH] == outcome.recipe_hash


def test_rebuild_is_skipped_by_recipe_hash(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """配方没变就整个跳过，连构建上下文都不打包。"""
    recipe = make_recipe("bench-test__cache__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)

    first = build_env_image(recipe, context=tmp_path / "ctx1", snapshot=snapshot)
    second = build_env_image(recipe, context=tmp_path / "ctx2", snapshot=snapshot)

    assert not first.skipped
    assert second.skipped
    assert second.image_id == first.image_id
    assert second.recipe_hash == first.recipe_hash
    # 跳过的那次没有构建日志，也没跑自查 —— 没建就没有新事实
    assert second.log == ""
    assert second.smoke is None


def test_force_rebuild_hits_the_layer_cache(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """`--force` 跳过配方哈希那层，但 docker 自己的层缓存还在，结果是同一个镜像。"""
    recipe = make_recipe("bench-test__force__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)

    first = build_env_image(recipe, context=tmp_path / "ctx1", snapshot=snapshot)
    forced = build_env_image(recipe, context=tmp_path / "ctx2", snapshot=snapshot, force=True)

    assert not forced.skipped
    assert forced.cache_hits > 0
    assert forced.image_id == first.image_id, "命中缓存就该产出同一个镜像"
    assert forced.duration_s < first.duration_s


def test_changing_the_snapshot_busts_the_cache(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """快照内容变了必须重建 —— 树哈希进了配方哈希，就是为了挡这个。"""
    recipe = make_recipe("bench-test__bust__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)

    first = build_env_image(recipe, context=tmp_path / "ctx1", snapshot=snapshot)
    moved = Snapshot(path=snapshot.path, commit=snapshot.commit, tree_sha="cafe" * 10, file_count=4)
    second = build_env_image(recipe, context=tmp_path / "ctx2", snapshot=moved)

    assert not second.skipped
    assert second.recipe_hash != first.recipe_hash


# ══════════════════════════════════════════════════════════════
# 构建失败与回收
# ══════════════════════════════════════════════════════════════


def test_failing_install_step_raises_with_the_log_tail(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    """装依赖失败要抛异常，而且报错里得带上日志末尾。

    §8.8 坑 ① 的教训：失败信号在一条我们没看的通道上时，20 个仓库会被整批报成
    "安装成功"。这里确认那条通道是通的。
    """
    recipe = EnvRecipe(
        environment_id="bench-test__boom__py311",
        repo_name="bench-test/boom",
        repo_url="golden://bench-test/boom",
        snapshot_commit="0" * 40,
        install_steps=("echo 这一步注定失败 && exit 3",),
        import_check=("shadowdemo",),
    )
    with pytest.raises(ImageBuildError) as exc_info:
        build_env_image(recipe, context=tmp_path / "ctx-boom", snapshot=snapshot)
    assert "这一步注定失败" in str(exc_info.value)


def test_gc_finds_and_removes_a_dead_environment(
    base_image_present: None,
    snapshot: Snapshot,
    tmp_path: Path,
    cleanup_images: list[str],
) -> None:
    """建一个镜像，然后在"活名单"里不提它 —— gc 应该认出来并且删得掉。"""
    recipe = make_recipe("bench-test__gcme__py311", source_roots=("src", ""))
    cleanup_images.append(recipe.image_tag)
    build_env_image(recipe, context=tmp_path / "ctx", snapshot=snapshot)

    # 活名单里有它 → 不动
    live = gc_candidates(live_environment_ids=[recipe.environment_id])
    assert recipe.environment_id not in [c.environment_id for c in live]

    # 活名单里没有 → 列为可回收
    dead = [
        c
        for c in gc_candidates(live_environment_ids=[])
        if c.environment_id == recipe.environment_id
    ]
    assert len(dead) == 1
    assert dead[0].size_bytes > 0

    assert remove_image(dead[0]) is None
    remaining = [
        c
        for c in gc_candidates(live_environment_ids=[])
        if c.environment_id == recipe.environment_id
    ]
    assert remaining == []


def test_import_check_script_is_in_the_base_image(base_image_present: None) -> None:
    """`bench-import-check` 必须在 PATH 上，自查全靠它。"""
    from app.sandbox.container import ContainerSpec, NetworkMode, Stage, build_env, run_in_container

    result = run_in_container(
        ContainerSpec(
            image=BASE_TAG,
            command=["bench-import-check", "--root", "/", "json"],
            timeout_s=60,
            stage=Stage.TEST,
            network=NetworkMode.NONE,
            env=build_env(),
            run_id="images-test",
        )
    )
    assert result.ok, result.stdout + result.stderr
    assert "json" in result.stdout


def test_home_and_tmpdir_are_on_disk_not_tmpfs(base_image_present: None) -> None:
    """§8.8 坑 ⑤：容器里的 HOME 和 TMPDIR 都不能落在 `/tmp`。

    `/tmp` 在评测容器里是 tmpfs，占的是内存额度；装 torch 那一套会直接
    `No space left on device`，看起来像"这个仓库装不上"。
    """
    from app.sandbox.container import ContainerSpec, NetworkMode, Stage, build_env, run_in_container

    result = run_in_container(
        ContainerSpec(
            image=BASE_TAG,
            command=["sh", "-c", 'echo "$HOME|$TMPDIR"; touch "$HOME/x" "$TMPDIR/y"'],
            timeout_s=60,
            stage=Stage.TEST,
            network=NetworkMode.NONE,
            env=build_env(),
            run_id="images-test",
        )
    )
    assert result.ok, result.stdout + result.stderr
    home, tmpdir = result.stdout.strip().split("|")
    assert not home.startswith("/tmp"), f"HOME 落在 tmpfs 上了：{home}"
    assert not tmpdir.startswith("/tmp"), f"TMPDIR 落在 tmpfs 上了：{tmpdir}"


def test_docker_cli_is_available() -> None:
    """这一组用例要 docker，缺了就该明确跳过而不是报一堆连接错误。"""
    if shutil.which("docker") is None:
        pytest.skip("没有 docker 命令")
