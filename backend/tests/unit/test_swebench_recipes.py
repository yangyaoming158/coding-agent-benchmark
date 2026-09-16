"""官方构建脚本的改写规则（E1-T7，`app.benchmark.swebench_recipes`）。

不起 docker。既用手工拼的最小脚本验每条规则，也把仓库里那份真实的
`datasets/swebench/build-specs.json` 整个过一遍，保证 50 道题都改写得出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.benchmark.swebench_recipes import (
    BASE_TAG,
    QHULL_TARBALL,
    RecipeError,
    base_context_files,
    build_context_files,
    env_context_files,
    env_tag,
    instance_dockerfile,
    instance_tag,
    load_build_specs,
    needs_qhull,
    rewrite_base_dockerfile,
    rewrite_env_dockerfile,
    rewrite_env_script,
    rewrite_repo_script,
    summarize,
)

SPECS_FILE = Path(__file__).resolve().parents[3] / "datasets" / "swebench" / "build-specs.json"

BASE = """\
FROM --platform=linux/x86_64 ubuntu:22.04
RUN apt update && apt install -y \\
wget \\
&& rm -rf /var/lib/apt/lists/*
RUN wget 'https://repo.anaconda.com/miniconda/Miniconda3-py311_23.11.0-2-Linux-x86_64.sh' \\
    -O miniconda.sh && bash miniconda.sh -b -p /opt/miniconda3
ENV PATH=/opt/miniconda3/bin:$PATH
RUN conda init --all
RUN conda config --append channels conda-forge
"""

REPO = """\
#!/bin/bash
set -euxo pipefail
git clone -o origin https://github.com/pallets/flask /testbed
chmod -R 777 /testbed
cd /testbed
git reset --hard 7ee9ceb71e868944a46e1ff00b506772a53a4f1d
git remote remove origin
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .
"""


def test_base_dockerfile_switches_every_download_to_tsinghua() -> None:
    text = rewrite_base_dockerfile(BASE)
    assert "repo.anaconda.com" not in text
    assert "mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py311" in text
    assert "sed -i 's|http://archive.ubuntu.com|http://mirrors.tuna.tsinghua.edu.cn|g" in text
    assert "ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in text
    assert "conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud" in text
    # .condarc 要写在 conda init 之前，官方随后 append conda-forge 才落到我们的映射上
    assert text.index(".condarc") < text.index("RUN conda init --all")
    assert text.endswith("RUN conda config --append channels conda-forge\n")


def test_base_dockerfile_refuses_unexpected_input() -> None:
    with pytest.raises(RecipeError):
        rewrite_base_dockerfile(BASE.replace("repo.anaconda.com", "example.com"))


def test_env_dockerfile_only_changes_from() -> None:
    official = (
        "FROM --platform=linux/x86_64 sweb.base.py.x86_64:latest\n\nCOPY ./setup_env.sh /root/\n"
    )
    text = rewrite_env_dockerfile(official)
    assert text.splitlines()[0] == f"FROM {BASE_TAG}"
    assert text.splitlines()[2:] == official.splitlines()[2:]


def test_repo_script_unpacks_local_tar_instead_of_cloning() -> None:
    text = rewrite_repo_script(REPO)
    assert "git clone" not in text
    assert "git reset --hard" not in text
    assert "git remote remove" not in text
    assert "tar -xf /root/repo.tar -C /testbed" in text
    assert "git init -q" in text and "commit -q -m base" in text
    # clone 之后的官方步骤原样保留
    assert "chmod -R 777 /testbed" in text
    assert "python -m pip install -e ." in text
    assert "QHULL" not in text
    # 2026 年的 pip/setuptools 跟官方镜像那一代对不上，构建时把隔离环境里的 setuptools 按回去
    assert "export PIP_CONSTRAINT=/root/pip-constraints.txt" in text
    assert "printf '%s\\n' 'setuptools<70' 'docutils<0.22' > /root/pip-constraints.txt" in text
    assert text.index("PIP_CONSTRAINT") < text.index("tar -xf /root/repo.tar")


def test_repo_script_drops_flag_removed_in_pip_25() -> None:
    official = REPO.replace(
        "python -m pip install -e .",
        "python -m pip install -v --no-use-pep517 --no-build-isolation -e .",
    )
    text = rewrite_repo_script(official)
    assert "--no-use-pep517" not in text
    assert "python -m pip install -v --no-build-isolation -e ." in text


def test_repo_script_copies_qhull_instead_of_wget() -> None:
    official = REPO.replace(
        "python -m pip install -e .",
        'QHULL_URL="http://www.qhull.org/download/qhull-2020-src-8.0.2.tgz"\n'
        'QHULL_TAR="/tmp/qhull-2020-src-8.0.2.tgz"\n'
        'wget -O "$QHULL_TAR" "$QHULL_URL"\n'
        "python -m pip install -e .",
    )
    assert needs_qhull(official)
    text = rewrite_repo_script(official)
    assert "wget" not in text
    assert f'cp /root/{QHULL_TARBALL} "$QHULL_TAR"' in text


def test_repo_script_refuses_unexpected_input() -> None:
    with pytest.raises(RecipeError):
        rewrite_repo_script(REPO.replace("git clone -o origin", "git clone"))


def test_env_script_adds_nodefaults_to_conda_forge_only_yml() -> None:
    official = (
        "cat <<'EOF' > environment.yml\nname: testbed\nchannels:\n  - conda-forge\n"
        "dependencies:\n  - numpy\nEOF\n"
    )
    text = rewrite_env_script(official)
    assert "channels:\n  - conda-forge\n  - nodefaults\ndependencies:" in text
    # 已经写了 nodefaults 的（xarray）和不用 conda env 的都原样
    already = official.replace("  - conda-forge\n", "  - conda-forge\n  - nodefaults\n")
    assert rewrite_env_script(already) == already
    assert rewrite_env_script("conda create -n testbed python=3.9 -y\n") == (
        "conda create -n testbed python=3.9 -y\n"
    )


def test_tags() -> None:
    assert env_tag("sweb.env.py.x86_64.34a2704c943706976d0871:latest") == (
        "bench-swebench-env:34a2704c943706976d0871"
    )
    assert instance_tag("pallets__flask-5014") == "bench-env:swebench__pallets__flask-5014"


def test_instance_dockerfile_pins_version_and_copies_inputs() -> None:
    text = instance_dockerfile("sweb.env.py.x86_64.abc:latest", version="3.7", with_qhull=True)
    assert text.startswith("FROM bench-swebench-env:abc\n")
    assert "COPY ./repo.tar /root/repo.tar" in text
    assert f"COPY ./{QHULL_TARBALL}" in text
    assert "ENV SETUPTOOLS_SCM_PRETEND_VERSION=3.7" in text
    assert "RUN rm -f /root/repo.tar" in text
    assert "COPY ./qhull" not in instance_dockerfile("x.y:latest", version="1", with_qhull=False)


@pytest.mark.skipif(not SPECS_FILE.exists(), reason="仓库里没有 build-specs.json")
def test_every_sampled_instance_rewrites_cleanly() -> None:
    """真实的 50 道：三层都改写得出来，一条 RecipeError 都不能有。"""
    specs = load_build_specs(SPECS_FILE)
    summary = summarize(specs)
    assert summary["instances"] == 50 and summary["envs"] == 20
    assert "repo.anaconda.com" not in base_context_files(specs)["Dockerfile"]
    for key in specs.envs:
        files = env_context_files(specs, key)
        assert files["Dockerfile"].startswith(f"FROM {BASE_TAG}")
        if "conda env create" in files["setup_env.sh"]:
            assert "  - nodefaults\n" in files["setup_env.sh"]
    for instance_id in specs.instances:
        files = build_context_files(specs, instance_id, version="1.0")
        assert "github.com" not in files["setup_repo.sh"]
        assert "wget" not in files["setup_repo.sh"]
        assert "--no-use-pep517" not in files["setup_repo.sh"]
    assert all(i.startswith("matplotlib") for i in summary["need_qhull"])
