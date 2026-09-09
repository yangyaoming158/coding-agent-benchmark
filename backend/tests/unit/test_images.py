"""镜像构建器里不需要 Docker 的那一半（E2-T3）。

配方解析、Dockerfile 渲染、配方哈希、磁盘水位、回收名单 —— 这些都是纯函数，
所以进 `make test`（不带 docker 标记那一批）。要起真容器的部分在
`tests/sandbox/test_images_docker.py`。

**这里盯得最紧的是渲染结果**：`.pth` 那一行嵌了三层引号，拆错行不会报错，
只会让工作区的源码目录没被插进 `sys.path` —— 而那正是"被测 AI 的补丁不生效"
这个不报错故障的成因。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.infrastructure.config import REPO_ROOT
from app.sandbox.images import (
    BASE_IMAGE_REF,
    BASE_TAG,
    DEFAULT_MIN_FREE_RATIO,
    LABEL_AGENT,
    LABEL_ENVIRONMENT_ID,
    LABEL_LAYER,
    LABEL_RECIPE_HASH,
    LAYER_AGENT,
    LAYER_BASE,
    LAYER_ENV,
    WORKSPACE_PTH_NAME,
    DiskHeadroom,
    DiskSpaceError,
    EnvRecipe,
    RecipeError,
    _stream_build,
    _truncate_log,
    agent_image_tag,
    compute_recipe_hash,
    disk_headroom,
    gc_candidates,
    load_recipe,
    load_recipes,
    parse_recipe,
    render_env_dockerfile,
    require_disk,
    workspace_pth_line,
)

RECIPES_DIR = REPO_ROOT / "images" / "envs"
BASE_DOCKERFILE = REPO_ROOT / "images" / "base" / "Dockerfile"

COMMIT = "b0cf5d15c9d1933b4f7741e3843e1a8c8495c35a"


def make_recipe(**overrides: Any) -> EnvRecipe:
    data: dict[str, Any] = {
        "environment_id": "demo__py311",
        "repo_name": "acme/demo",
        "repo_url": "https://example.invalid/acme/demo.git",
        "snapshot_commit": COMMIT,
        "install_steps": ("python -m pip install -e .",),
        "import_check": ("demo",),
    }
    data.update(overrides)
    return EnvRecipe(**data)


# ══════════════════════════════════════════════════════════════
# 配方
# ══════════════════════════════════════════════════════════════


def test_checked_in_recipes_all_parse() -> None:
    """仓库里那几份配方必须能加载。

    它们是构建器唯一的输入，坏了不会在 CI 里以别的方式暴露 ——
    只会在有人跑 `bench images build` 时才炸。
    """
    recipes = load_recipes(RECIPES_DIR)
    assert len(recipes) >= 4
    ids = [r.environment_id for r in recipes]
    assert ids == sorted(ids), "load_recipes 要按 environment_id 排序返回"
    assert "bench-golden__auth__py311" in ids


def test_unknown_field_is_rejected() -> None:
    """多写一个字段直接报错，不是悄悄忽略。

    悄悄忽略的话，把 `install_steps` 拼成 `install_step` 会表现成"什么都没装" ——
    镜像照样建得出来，跑第一道题才发现依赖不在。
    """
    with pytest.raises(RecipeError, match="不认识的字段"):
        parse_recipe(
            {
                "environment_id": "demo__py311",
                "repo_name": "acme/demo",
                "repo_url": "https://example.invalid/x.git",
                "snapshot_commit": COMMIT,
                "install_step": ["pip install -e ."],
            }
        )


def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(RecipeError, match="缺少必填字段"):
        parse_recipe({"environment_id": "demo__py311"})


@pytest.mark.parametrize("bad", ["acme/demo", "-leading-dash", ".dot", "中文环境", ""])
def test_environment_id_must_be_a_valid_tag(bad: str) -> None:
    """环境 id 直接当镜像 tag 用，非法字符要在加载配方时就拦下。"""
    with pytest.raises(RecipeError, match="不能直接当镜像 tag"):
        make_recipe(environment_id=bad)


def test_snapshot_commit_must_be_full_sha() -> None:
    """短 sha 不行：它不唯一，两个月后可能指向另一个提交。"""
    with pytest.raises(RecipeError, match="snapshot_commit"):
        make_recipe(snapshot_commit="b0cf5d1")


def test_waiving_checks_requires_a_reason() -> None:
    """关掉自查必须写理由 —— 不写理由的豁免，三周后没人知道当初为什么放行。"""
    with pytest.raises(RecipeError, match="checks_waived_because"):
        make_recipe(import_check=())
    with pytest.raises(RecipeError, match="checks_waived_because"):
        make_recipe(collect_check=False)
    # 写了理由就放行
    assert make_recipe(collect_check=False, checks_waived_because="上游收集本来就报错")


def test_filename_must_match_environment_id(tmp_path: Path) -> None:
    """文件名和里面的 id 对不上，就没法从环境 id 直接找到配方。"""
    payload = {
        "environment_id": "demo__py311",
        "repo_name": "acme/demo",
        "repo_url": "https://example.invalid/x.git",
        "snapshot_commit": COMMIT,
        "import_check": ["demo"],
    }
    path = tmp_path / "something-else.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecipeError, match="文件名和 environment_id 对不上"):
        load_recipe(path)


def test_tuple_fields_must_be_string_lists() -> None:
    with pytest.raises(RecipeError, match="install_steps 要是字符串列表"):
        parse_recipe(
            {
                "environment_id": "demo__py311",
                "repo_name": "acme/demo",
                "repo_url": "https://example.invalid/x.git",
                "snapshot_commit": COMMIT,
                "install_steps": "pip install -e .",
            }
        )


def test_test_args_default_to_collecting_everything() -> None:
    """默认从工作区根收全部 —— 多数仓库这样是对的。"""
    assert make_recipe().test_args == ()


def test_test_args_must_be_a_string_list() -> None:
    with pytest.raises(RecipeError, match="test_args 要是字符串列表"):
        parse_recipe(
            {
                "environment_id": "demo__py311",
                "repo_name": "acme/demo",
                "repo_url": "https://example.invalid/x.git",
                "snapshot_commit": COMMIT,
                "import_check": ["demo"],
                "test_args": "tests/",
            }
        )


def test_image_tag_is_derived_from_environment_id() -> None:
    assert make_recipe().image_tag == "bench-env:demo__py311"


# ══════════════════════════════════════════════════════════════
# sys.path 前插
# ══════════════════════════════════════════════════════════════


def test_pth_line_puts_src_before_workspace_root() -> None:
    """插进去之后的顺序必须和配方里写的一致。

    `sys.path.insert(0, p)` 是逐个插到最前面，按原顺序插完最后一个反而排在最前 ——
    所以实现里用了 `reversed()`。这条用例盯的就是那个 `reversed()`：
    去掉它不会报错，只会让 src 布局的仓库解析到工作区根，而工作区根底下
    根本没有那个包名，于是悄悄落回镜像里的快照。
    """
    line = workspace_pth_line(("src", ""))
    assert line.startswith("import sys, os;")
    assert '["/workspace/src", "/workspace"]' in line
    assert "reversed(" in line
    assert "os.path.isdir(p)" in line


def test_pth_line_handles_empty_root_as_workspace_itself() -> None:
    assert '["/workspace"]' in workspace_pth_line(("",))


def test_pth_line_is_valid_python_and_orders_paths(tmp_path: Path) -> None:
    """把生成的那一行真的执行一遍，确认最终 `sys.path` 顺序是对的。

    只做字符串断言挡不住"语法对但语义反了"—— 这一行是要被 `site` 模块 exec 的，
    这里就照样 exec 一次。
    """
    line = workspace_pth_line(("src", ""))
    (tmp_path / "workspace" / "src").mkdir(parents=True)
    namespace: dict[str, Any] = {}
    # 换成临时目录，免得依赖真实的 /workspace 存不存在
    exec(line.replace("/workspace", str(tmp_path / "workspace")), namespace)
    import sys

    assert sys.path[0] == str(tmp_path / "workspace" / "src")
    # 工作区根不存在的那一层不该被插进去（isdir 守卫）
    assert str(tmp_path / "workspace") not in sys.path[:1]
    sys.path.remove(str(tmp_path / "workspace" / "src"))


# ══════════════════════════════════════════════════════════════
# Dockerfile 渲染
# ══════════════════════════════════════════════════════════════


def test_render_is_deterministic() -> None:
    """同一份配方永远渲染出同一段文本 —— 它要进配方哈希，不确定就等于缓存失效。"""
    recipe = make_recipe()
    assert render_env_dockerfile(recipe) == render_env_dockerfile(recipe)


def test_render_contains_the_four_required_steps() -> None:
    text = render_env_dockerfile(make_recipe())
    assert f"FROM {BASE_TAG}" in text
    assert "COPY repo /opt/repo" in text
    assert "RUN python -m pip install -e ." in text
    assert WORKSPACE_PTH_NAME in text
    assert "pip freeze --all > /opt/bench/requirements.lock" in text
    assert text.rstrip().endswith("WORKDIR /workspace")


def test_render_skips_apt_block_when_no_extra_packages() -> None:
    """没有额外系统包就不该有 apt 步骤 —— 多一层就多一层缓存要维护。"""
    assert "apt-get" not in render_env_dockerfile(make_recipe())


def test_render_apt_block_bypasses_the_proxy() -> None:
    """apt 必须显式绕开代理。

    dockerd 会把它自己那份代理注进每个构建步骤（2026-09-08 实测），而走代理拉
    deb.debian.org 是 9.2 秒一个请求、直连 1.4 秒 —— images/claude-code 那份
    Dockerfile 第一次构建就因此挂了 14 分钟一个包都没下完。
    """
    text = render_env_dockerfile(make_recipe(apt_packages=("libpq-dev", "libxml2-dev")))
    assert "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get update" in text
    # 包名排序，渲染结果才和配方里的书写顺序无关
    assert "--no-install-recommends libpq-dev libxml2-dev" in text


def test_render_keeps_install_steps_in_order() -> None:
    """装依赖的顺序有意义：先 setuptools 再 --no-build-isolation 的梯子不能被打乱。"""
    text = render_env_dockerfile(make_recipe(install_steps=("step-one", "step-two", "step-three")))
    positions = [text.index(f"RUN {s}") for s in ("step-one", "step-two", "step-three")]
    assert positions == sorted(positions)


def test_base_dockerfile_pins_the_same_digest() -> None:
    """`images/base/Dockerfile` 的 FROM 和 `BASE_IMAGE_REF` 必须是同一个 digest。

    两处不一致时构建照样能成功，只是配方哈希算的是一个和实际底座无关的值 ——
    换了底座却不重建，而没有任何提示。
    """
    text = BASE_DOCKERFILE.read_text(encoding="utf-8")
    assert f"FROM {BASE_IMAGE_REF}" in text


def test_base_dockerfile_keeps_home_and_tmpdir_off_tmpfs() -> None:
    """§8.8 坑 ⑤：HOME 和 TMPDIR 都不能落在 `/tmp`，那是 tmpfs、吃内存额度。

    这一条踩了两次（只挪一个不够），所以钉一条用例守着。
    """
    text = BASE_DOCKERFILE.read_text(encoding="utf-8")
    assert "ENV HOME=/home/bench" in text
    assert "TMPDIR=/var/tmp/bench" in text
    assert "HOME=/tmp" not in text


# ══════════════════════════════════════════════════════════════
# 配方哈希
# ══════════════════════════════════════════════════════════════


def _hash(**overrides: Any) -> str:
    recipe = overrides.pop("recipe", make_recipe())
    kwargs: dict[str, Any] = {
        "dockerfile": render_env_dockerfile(recipe),
        "base_ref": "sha256:" + "ab" * 32,
        "snapshot_tree_sha": "cd" * 20,
        "recipe": recipe,
    }
    kwargs.update(overrides)
    return compute_recipe_hash(**kwargs)


def test_recipe_hash_is_stable() -> None:
    assert _hash() == _hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_ref": "sha256:" + "ef" * 32},
        {"snapshot_tree_sha": "00" * 20},
        {"dockerfile": "FROM scratch"},
    ],
    ids=["底座换了", "快照换了", "Dockerfile 换了"],
)
def test_recipe_hash_changes_when_any_input_changes(overrides: dict[str, Any]) -> None:
    assert _hash(**overrides) != _hash()


def test_recipe_hash_reacts_to_smoke_settings() -> None:
    """自查项不进 Dockerfile，但它决定"这次构建算不算数"，所以也要进哈希。

    不进的话，把 import_check 从空改成有内容之后重建，会直接命中"已是最新"跳过，
    自查一次都不会跑。
    """
    waived = make_recipe(import_check=(), checks_waived_because="临时")
    assert _hash(recipe=waived) != _hash()


def test_recipe_hash_reacts_to_test_args() -> None:
    """`test_args` 决定自查按什么口径收用例，所以也要进哈希。

    不进的话，把收集范围从"整个仓库"改成"仓库自己的 tests/"之后重建会直接命中跳过，
    等于改了判据却没重新验过。
    """
    narrowed = make_recipe(test_args=("--import-mode=importlib", "tests/"))
    assert _hash(recipe=narrowed) != _hash()


def test_recipe_hash_ignores_the_note() -> None:
    """`note` 是给人看的备注，改一句话不该让所有镜像重建。"""
    assert _hash(recipe=make_recipe(note="改了备注")) == _hash()


# ══════════════════════════════════════════════════════════════
# 构建事件流
# ══════════════════════════════════════════════════════════════


def test_stream_build_counts_cache_hits_and_steps() -> None:
    log, cache_hits, steps, error = _stream_build(
        iter(
            [
                {"stream": "Step 1/3 : FROM bench-base:py311"},
                {"stream": " ---> Using cache\n"},
                {"stream": "Step 2/3 : RUN pip install -e .\n"},
                {"stream": " ---> Using cache\n"},
                {"stream": "Step 3/3 : WORKDIR /workspace\n"},
                {"stream": "Successfully built abc123\n"},
            ]
        )
    )
    assert (cache_hits, steps, error) == (2, 3, None)
    assert "Successfully built" in log


def test_stream_build_reports_the_error_event() -> None:
    """出错事件之后流照样正常结束，不抛异常 —— 漏了这一步，构建失败会被当成成功。

    这是 §8.8 坑 ① 的同一类问题：失败的信号在一条我们没看的通道上。
    """
    log, _cache_hits, _steps, error = _stream_build(
        iter(
            [
                {"stream": "Step 1/2 : RUN false\n"},
                {"error": "The command '/bin/sh -c false' returned a non-zero code: 1"},
            ]
        )
    )
    assert error is not None
    assert "non-zero code: 1" in error
    assert "[error]" in log


def test_truncate_log_keeps_both_ends() -> None:
    """构建失败的原因在最后，装了什么在最前，中间是噪音。"""
    text = "A" * 100 + "B" * 10_000 + "C" * 100
    out, truncated = _truncate_log(text, limit=400)
    assert truncated
    assert out.startswith("A")
    assert out.endswith("C")
    assert "中间省略" in out


def test_truncate_log_leaves_short_logs_alone() -> None:
    out, truncated = _truncate_log("短日志", limit=4096)
    assert (out, truncated) == ("短日志", False)


# ══════════════════════════════════════════════════════════════
# 磁盘水位
# ══════════════════════════════════════════════════════════════


def test_require_disk_passes_above_the_threshold() -> None:
    headroom = DiskHeadroom(path=Path("/var/lib/docker"), total_bytes=1000, free_bytes=200)
    require_disk(headroom, min_free_ratio=DEFAULT_MIN_FREE_RATIO)


def test_require_disk_refuses_below_the_threshold() -> None:
    """拦在开建之前。写满之后 daemon 自己会开始报错，那时候的错误信息指不到真正的原因。"""
    headroom = DiskHeadroom(path=Path("/var/lib/docker"), total_bytes=1000, free_bytes=100)
    with pytest.raises(DiskSpaceError, match="磁盘剩余不足"):
        require_disk(headroom, min_free_ratio=0.15)


def test_disk_headroom_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """目录不存在就往上找，不要直接抛异常 —— 抛了水位检查就等于永远拦不住。"""
    headroom = disk_headroom(tmp_path / "还没建" / "更深一层")
    assert headroom.total_bytes > 0
    assert 0.0 <= headroom.free_ratio <= 1.0


def test_disk_headroom_ratio_of_empty_disk_is_zero() -> None:
    assert DiskHeadroom(path=Path("/"), total_bytes=0, free_bytes=0).free_ratio == 0.0


# ══════════════════════════════════════════════════════════════
# 回收
# ══════════════════════════════════════════════════════════════


def fake_image(
    image_id: str,
    *,
    tags: list[str] | None = None,
    layer: str | None = None,
    environment_id: str | None = None,
    agent: str | None = None,
    digest: str | None = None,
    bench: bool = True,
    size: int = 2**30,
    disk_size: int | None = None,
    shared_size: int = 0,
) -> Any:
    labels: dict[str, str] = {}
    if bench:
        labels["bench.owner"] = "coding-agent-benchmark"
    if layer:
        labels[LABEL_LAYER] = layer
    if environment_id:
        labels[LABEL_ENVIRONMENT_ID] = environment_id
    if agent:
        labels[LABEL_AGENT] = agent
    attrs: dict[str, Any] = {"Config": {"Labels": labels}, "Size": size}
    if disk_size is not None:
        attrs["_DiskSize"] = disk_size
    if shared_size:
        attrs["_SharedSize"] = shared_size
    if digest:
        attrs["RepoDigests"] = [f"bench-env@{digest}"]
    return SimpleNamespace(id=image_id, tags=tags or [], attrs=attrs)


class FakeImages:
    """只实现 `list`，`gc_candidates` 用得到的就这一个。"""

    def __init__(self, images: list[Any]) -> None:
        self.images = images
        self.filters: dict[str, Any] | None = None

    def list(self, *, filters: dict[str, Any] | None = None) -> list[Any]:
        self.filters = filters
        return self.images


class FakeApi:
    """`/system/df` 的替身。

    真 daemon 在 containerd 存储下有两个不一样的 Size：`images.list()` 里那个是
    压缩后的内容大小，`/system/df` 里那个才是磁盘上的实际占用，实测差三四倍。
    替身把两个都造出来，免得测试只覆盖到错的那一个。
    """

    def __init__(self, images: list[Any]) -> None:
        self._images = images

    def df(self) -> dict[str, Any]:
        return {
            "Images": [
                {
                    "Id": img.id,
                    "Size": img.attrs.get("_DiskSize", img.attrs.get("Size", 0)),
                    "SharedSize": img.attrs.get("_SharedSize", 0),
                }
                for img in self._images
            ]
        }


class FakeDocker:
    def __init__(self, images: list[Any]) -> None:
        self.images = FakeImages(images)
        self.api = FakeApi(images)


def test_gc_never_touches_the_base_layer() -> None:
    """第一层永远保留：它是所有 env 镜像的底座，删了全部要重建。

    保住它靠的是"有 tag 但没有环境 id"这一条，**不是** `bench.layer` 标签 ——
    label 会被子镜像继承，建 env 时产生的中间层顶着从 bench-base 继承来的
    `bench.layer=base`，靠它判断的话 gc 永远收敛不掉（2026-09-08 实测）。
    """
    client = FakeDocker([fake_image("sha256:base", tags=[BASE_TAG], layer=LAYER_BASE)])
    assert gc_candidates(live_environment_ids=[], client=client) == []


def test_gc_ignores_the_inherited_layer_label_on_untagged_images() -> None:
    """没有 tag 的中间层即使顶着 `bench.layer=base`，也照收不误。"""
    client = FakeDocker(
        [fake_image("sha256:mid", tags=[], layer=LAYER_BASE, environment_id="live")]
    )
    candidates = gc_candidates(live_environment_ids=["live"], client=client)
    assert [c.image_id for c in candidates] == ["sha256:mid"]


def test_gc_collects_superseded_untagged_builds_of_live_environments() -> None:
    """环境还活着，但被新版本顶掉的旧构建丢了 tag —— 那就是没人引用了。

    不收的话，每重建一次就攒下一堆：实测跑了几轮之后 `images list` 里躺着
    10 个无标签镜像、约 1 GiB（2026-09-08）。
    """
    client = FakeDocker(
        [
            fake_image("sha256:new", tags=["bench-env:live"], environment_id="live"),
            fake_image("sha256:old", tags=[], environment_id="live"),
        ]
    )
    candidates = gc_candidates(live_environment_ids=["live"], client=client)
    assert [c.image_id for c in candidates] == ["sha256:old"]
    assert "旧版本" in candidates[0].reason


def test_gc_keeps_untagged_images_whose_digest_is_recorded() -> None:
    """digest 还在 `environment_specs` 里记着的，一律不删。

    协议 C-36 要求运行记录按 digest 引用镜像。删掉一个还被记着的 digest，
    等于把那次实验的可复现性抹掉 —— 而且不会有任何报错。
    """
    kept = "sha256:" + "ab" * 32
    client = FakeDocker([fake_image("sha256:old", tags=[], environment_id="live", digest=kept)])
    assert gc_candidates(live_environment_ids=["live"], keep_digests=[kept], client=client) == []
    # 没传 keep_digests 时它就是可回收的 —— 证明上面那条是 keep_digests 起的作用
    assert gc_candidates(live_environment_ids=["live"], client=client) != []


def test_gc_keeps_untagged_images_by_image_id_too() -> None:
    """本机构建的镜像没推过仓库时 digest 就是 image id，两种写法都要认。"""
    client = FakeDocker([fake_image("sha256:old", tags=[], environment_id="live")])
    assert (
        gc_candidates(live_environment_ids=["live"], keep_digests=["sha256:old"], client=client)
        == []
    )


def test_gc_keeps_live_environments_and_their_agent_layers() -> None:
    client = FakeDocker(
        [
            fake_image("sha256:a", tags=["bench-env:live"], layer=LAYER_ENV, environment_id="live"),
            fake_image(
                "sha256:b",
                tags=["bench-agent:live-aider"],
                layer=LAYER_AGENT,
                environment_id="live",
                agent="aider",
            ),
        ]
    )
    assert gc_candidates(live_environment_ids=["live"], client=client) == []


def test_gc_lists_environments_that_no_longer_exist() -> None:
    client = FakeDocker(
        [
            fake_image("sha256:a", tags=["bench-env:live"], layer=LAYER_ENV, environment_id="live"),
            fake_image("sha256:b", tags=["bench-env:gone"], layer=LAYER_ENV, environment_id="gone"),
        ]
    )
    candidates = gc_candidates(live_environment_ids=["live"], client=client)
    assert [c.tags[0] for c in candidates] == ["bench-env:gone"]
    assert "已经没有配方" in candidates[0].reason


def test_gc_only_looks_at_images_we_labelled() -> None:
    """手工 `make images` 建的那几个没有 bench 标签，不能被碰到。

    回收命令宁可少删，也不能误删别的项目的东西 —— 重建一个重依赖的环境镜像
    要十几分钟，而误删是不可逆的。
    """
    client = FakeDocker([])
    gc_candidates(live_environment_ids=[], client=client)
    assert client.images.filters == {"label": "bench.owner=coding-agent-benchmark"}


def test_gc_skips_labelled_images_it_cannot_attribute() -> None:
    """有标签、但认不出属于哪个环境的镜像，不敢删。"""
    client = FakeDocker([fake_image("sha256:x", tags=["bench-env:mystery"], layer=LAYER_ENV)])
    assert gc_candidates(live_environment_ids=[], client=client) == []


def test_gc_reports_what_deleting_actually_frees() -> None:
    """报的是"删了能腾出多少"，不是"这个镜像总共多大"。

    两处都容易搞错：一是 `images.list()` 那个 `Size` 在 containerd 存储下报的是
    **压缩后**的大小，和磁盘上差三四倍；二是镜像之间共用底层，把总大小相加会把
    共用的部分重复算好几遍。装了 torch 的那个镜像总大小 10.51 GiB、独占 9.73 GiB，
    底座 0.78 GiB 是和另外七个共用的 —— 删它一个腾不出 10.51。
    """
    client = FakeDocker(
        [
            fake_image(
                "sha256:heavy",
                tags=["bench-env:gone"],
                environment_id="gone",
                size=3 * 2**30,  # images.list() 报的（压缩后）
                disk_size=10 * 2**30,  # /system/df 报的（磁盘上）
                shared_size=2 * 2**30,  # 其中和别人共用的
            )
        ]
    )
    candidate = gc_candidates(live_environment_ids=[], client=client)[0]
    assert candidate.size_bytes == 8 * 2**30
    assert "独占 8.00 GiB" in candidate.display


def test_gc_falls_back_when_the_daemon_has_no_df() -> None:
    """拿不到 `/system/df` 就退回压缩后的大小，不要因此崩掉整条回收。"""
    client = FakeDocker([fake_image("sha256:x", tags=["bench-env:gone"], environment_id="gone")])
    client.api = None  # type: ignore[assignment]
    assert gc_candidates(live_environment_ids=[], client=client)[0].size_bytes == 2**30


def test_gc_collects_dangling_images_we_created() -> None:
    """没标签也没环境 id 的悬空镜像是构建过程留下的，可以删。"""
    client = FakeDocker([fake_image("sha256:dangling", tags=[], layer=LAYER_ENV)])
    candidates = gc_candidates(live_environment_ids=[], client=client)
    assert len(candidates) == 1
    assert "悬空" in candidates[0].reason
    assert "<无标签>" in candidates[0].display


# ══════════════════════════════════════════════════════════════
# 其他
# ══════════════════════════════════════════════════════════════


def test_agent_image_tag_shape() -> None:
    assert agent_image_tag("sqlfluff__py311", "aider") == "bench-agent:sqlfluff__py311-aider"


def test_agent_image_tag_rejects_illegal_names() -> None:
    with pytest.raises(RecipeError, match="不合法"):
        agent_image_tag("has/slash", "aider")


def test_build_outcome_prefers_digest_over_tag() -> None:
    """协议 C-36：引用镜像用 digest 不用 tag。"""
    from app.sandbox.images import BuildOutcome

    outcome = BuildOutcome(
        tag="bench-env:demo__py311",
        layer=LAYER_ENV,
        image_id="sha256:" + "ab" * 32,
        digest="sha256:" + "cd" * 32,
        recipe_hash="x",
        skipped=False,
        cache_hits=0,
        steps=1,
        duration_s=1.0,
        log="",
    )
    assert outcome.image_ref == "bench-env@sha256:" + "cd" * 32
    # 没有 digest 时退回 tag，如实反映"这个镜像没有内容地址"
    assert dataclasses.replace(outcome, digest=None).image_ref == outcome.tag


def test_recipe_labels_are_free_of_timestamps() -> None:
    """镜像标签里不能有时间戳。

    label 是镜像配置的一部分，带时间戳的话每次构建都会产生一个新的 image id，
    "重复构建命中缓存"这条验收标准就再也观察不到了。
    """
    from app.sandbox import images as images_module

    source = Path(images_module.__file__).read_text(encoding="utf-8")
    assert "LABEL_BUILT_AT" not in source
    assert LABEL_RECIPE_HASH == "bench.recipe_hash"
