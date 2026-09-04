"""Bare mirror 管理（E2-T1 的前半段）。

镜像是"运行时不联网 clone"的兑现方式。这一组用例盯两件事：
路径是不是被外部数据牵着走（`repo_name` 来自 GitHub 挖掘），
以及"commit 不在镜像里"这条路径有没有给出能查的错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.mirror import (
    CommitNotFoundError,
    MirrorError,
    MirrorManager,
    mirror_dir_name,
    validate_commit,
    validate_repo_name,
)
from tests.sandbox.conftest import SourceRepo, commit_all, write

REPO_NAME = "example/demo"


@pytest.fixture
def mirrors(tmp_path: Path) -> MirrorManager:
    return MirrorManager(tmp_path / "mirrors")


# ── 名字和 SHA 的校验 ───────────────────────────────────────


def test_mirror_dir_name_flattens_owner_and_repo() -> None:
    assert mirror_dir_name("psf/requests") == "psf__requests.git"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "owner/../repo",
        "owner",
        "owner/repo/extra",
        "owner/re po",
        "/owner/repo",
        "",
    ],
)
def test_repo_name_rejects_bad_input(bad: str) -> None:
    """`repo_name` 是外部数据，会被拼进目录名，穿越写法必须在这里断掉。"""
    with pytest.raises(MirrorError):
        validate_repo_name(bad)


@pytest.mark.parametrize(
    "good", ["psf/requests", "pallets/flask", "python-jsonschema/jsonschema", "a.b/c_d-e"]
)
def test_repo_name_accepts_real_names(good: str) -> None:
    assert validate_repo_name(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "abc123",  # 短 SHA
        "main",  # 分支名
        "v1.2.3",  # tag
        "A" * 40,  # 大写
        "0" * 39,  # 少一位
        "",
    ],
)
def test_commit_must_be_full_lowercase_sha(bad: str) -> None:
    with pytest.raises(MirrorError, match="40 位"):
        validate_commit(bad)


# ── clone / fetch / ensure_commit ──────────────────────────


def test_clone_creates_bare_mirror(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    path = mirrors.clone(REPO_NAME, str(source_repo.path))
    assert path == mirrors.path_for(REPO_NAME)
    assert mirrors.exists(REPO_NAME)
    # bare 仓库：没有工作树，对象直接放在仓库根下
    assert not (path / ".git").exists()
    assert (path / "objects").is_dir()


def test_clone_is_idempotent(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    """镜像已经在了就直接返回，不重拉 —— 一个大仓库重拉一次是几分钟。"""
    first = mirrors.clone(REPO_NAME, str(source_repo.path))
    marker = first / "objects" / "info" / "bench-marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("在这儿\n", encoding="utf-8")

    mirrors.clone(REPO_NAME, str(source_repo.path))
    assert marker.exists(), "镜像被重新拉了一遍"


def test_exists_is_false_for_half_written_dir(mirrors: MirrorManager) -> None:
    """clone 中途失败留下的空壳目录不算数，否则后面每条命令都会莫名其妙地失败。"""
    mirrors.path_for(REPO_NAME).mkdir(parents=True)
    assert not mirrors.exists(REPO_NAME)


def test_has_commit(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    mirrors.clone(REPO_NAME, str(source_repo.path))
    assert mirrors.has_commit(REPO_NAME, source_repo.base_commit)
    assert mirrors.has_commit(REPO_NAME, source_repo.fix_commit)
    assert not mirrors.has_commit(REPO_NAME, "0" * 40)


def test_has_commit_rejects_tree_sha(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    """树对象存在不等于提交存在。`^{commit}` 那一段就是为这个加的。"""
    mirrors.clone(REPO_NAME, str(source_repo.path))
    tree_sha = mirrors.tree_sha(REPO_NAME, source_repo.base_commit)
    assert not mirrors.has_commit(REPO_NAME, tree_sha)


def test_tree_sha_matches_upstream(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    mirrors.clone(REPO_NAME, str(source_repo.path))
    assert len(mirrors.tree_sha(REPO_NAME, source_repo.base_commit)) == 40


def test_ensure_commit_clones_when_missing(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    assert not mirrors.exists(REPO_NAME)
    path = mirrors.ensure_commit(REPO_NAME, str(source_repo.path), source_repo.base_commit)
    assert path.is_dir()
    assert mirrors.has_commit(REPO_NAME, source_repo.base_commit)


def test_ensure_commit_fetches_new_commits(mirrors: MirrorManager, source_repo: SourceRepo) -> None:
    """镜像拉过之后上游又有了新提交，`ensure_commit` 会自己 fetch 一次。"""
    mirrors.clone(REPO_NAME, str(source_repo.path))
    write(source_repo.path, "src/extra.py", "X = 1\n")
    newer = commit_all(source_repo.path, "feat: 又加了点东西")
    assert not mirrors.has_commit(REPO_NAME, newer)

    mirrors.ensure_commit(REPO_NAME, str(source_repo.path), newer)
    assert mirrors.has_commit(REPO_NAME, newer)


def test_ensure_commit_raises_for_unknown_sha(
    mirrors: MirrorManager, source_repo: SourceRepo
) -> None:
    """fetch 过还是没有，说明题目里的 base_commit 就是错的，报清楚原因。"""
    with pytest.raises(CommitNotFoundError) as caught:
        mirrors.ensure_commit(REPO_NAME, str(source_repo.path), "0" * 40)
    assert "force-push" in str(caught.value)


def test_fetch_requires_existing_mirror(mirrors: MirrorManager) -> None:
    with pytest.raises(MirrorError, match="先 clone"):
        mirrors.fetch(REPO_NAME)


def test_rejects_option_like_url(mirrors: MirrorManager) -> None:
    """以 `-` 开头的 URL 会被 git 当成选项。`repo_url` 是外部数据，必须挡。"""
    with pytest.raises(MirrorError, match="不能以 - 开头"):
        mirrors.clone(REPO_NAME, "--upload-pack=whatever")
