"""按官方配方在本机建 SWE-bench 镜像：把官方构建脚本改成走国内源（E1-T7，§8.6 的"退回自建"）。

## 为什么要有这条路

官方镜像是从 Docker Hub 拉的，50 个去重后 38.9 GB。这台机器出境的线路 0.06–15 MB/s 乱晃
还会整条卡死（2026-09-15/16 实测，细账在 `03-benchmark-spec.md` §8.6）。而官方镜像本来就是
从一份公开的构建脚本建出来的（swebench 包的 `MAP_REPO_VERSION_TO_SPECS`），脚本里
要下载的东西 —— miniconda 安装包、conda 包、pip 包、apt 包 —— **清华源全都有**，国内直连
几十 MB/s。所以与其等镜像，不如把脚本改成走国内源，在本机建。

产物和官方镜像的差别：**配方相同，二进制不同**。官方脚本里 pip 依赖是钉死版本的，
conda 那部分（`conda create python=3.x`、少数几个 `conda env create`）不钉，所以建出来的
包版本可能和官方发布的那份镜像有出入。这和官方 harness 自己本地建（`run_evaluation`
不带 `--namespace` 时就是本地建）是一回事，报告里如实写"按官方配方本机建"。

## 官方脚本原样在 `datasets/swebench/build-specs.json`，这里只做四处改写

1. **base 镜像**：apt 源、miniconda 下载地址换清华；写 `.condarc` 让 conda 走清华；
   `PIP_INDEX_URL` 指清华。
2. **env 镜像**：`FROM` 换成我们的 base 标签，脚本一字不改（源已经在 base 里换好）。
3. **instance 镜像**：`git clone` GitHub 换成解开本地 git 镜像导出的 tar 再 `git init`
   提交一次 —— 顺带把 `/testbed` 里的 git 历史剥干净（官方镜像里那份是全史，见 §8.6 第五节）；
   `git reset` / `git remote remove` 两行随之删掉；matplotlib 要的 qhull 源码包从本地拷，
   不再 `wget`。
4. 其余一行不动。`sed` 改 setup.py、`apt-get install texlive`、`pip install -e .` 全按官方来。

2026-09-16 晚抽到 75 道后又补了两条（§8.6 七点六第 6、7 条），都是"官方镜像那一代"和"今天"的差距：

5. **matplotlib 的 FreeType 源码包预先放进它自己的下载缓存。** `setup.py` 会去 sourceforge /
   savannah 下 `freetype-2.6.1.tar.gz`（和 qhull 一样是官方脚本之外、构建时才发生的下载），
   走代理一次卡死 17 分钟、一次直接失败。它下载前先看 `~/.cache/matplotlib/<sha256>`，
   命中就不联网，所以把包 `COPY` 到那个位置就行，脚本一字不改。
6. **仓库没有 `setup.py` 的，构建前把 pip 按回 24。** pylint 2.15 只有 `pyproject.toml` +
   `setup.cfg`，声明的 `setuptools~=62.6` 没有 PEP 660 的 `build_editable`；官方那一代的
   pip 24 会退回 `setup.py develop`（用 setup.cfg 顶上），pip 25 把这条退路删了，26 直接报错。
   只对没有 `setup.py` 的仓库加这一行（有 setup.py 的走另一条路，pip 26 还认），已建好的镜像不重建。

改写全是纯字符串函数，测试里不起 docker 就能验。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 清华源。apt 用 http（base 镜像装 ca-certificates 之前就要用），其余 https。
TSINGHUA_APT = "http://mirrors.tuna.tsinghua.edu.cn"
TSINGHUA_PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"
TSINGHUA_MINICONDA = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda"
TSINGHUA_CONDA_MAIN = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main"
TSINGHUA_CONDA_R = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r"
TSINGHUA_CONDA_CLOUD = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud"
#: 建镜像时这些主机不走代理：走了反而慢（代理是出境线路）。
MIRROR_HOSTS = ("mirrors.tuna.tsinghua.edu.cn", "pypi.tuna.tsinghua.edu.cn")

#: 我们这边的镜像标签。base 一个、env 按官方的 env key 一个、instance 按题一个。
#: instance 用 `bench-env:<environment_id>`，和 `cli.images build` 给自建环境起名的规矩一致。
BASE_TAG = "bench-swebench-base:ubuntu22.04-py311"
ENV_TAG_PREFIX = "bench-swebench-env"


def env_tag(env_image_key: str) -> str:
    """`sweb.env.py.x86_64.<hash>:latest` → `bench-swebench-env:<hash>`。

    官方的 key 尾巴上带 `:latest`，先剥掉，不然拼出来的标签有两个冒号，docker 不认。
    """
    key = env_image_key.split(":", 1)[0]
    return f"{ENV_TAG_PREFIX}:{key.rsplit('.', 1)[-1]}"


def instance_tag(instance_id: str) -> str:
    return f"bench-env:swebench__{instance_id}"


#: `.condarc`：defaults 和 conda-forge 都指到清华。官方 base 里那句
#: `conda config --append channels conda-forge` 照跑，会落到 custom_channels 那条映射上。
CONDARC = f"""\
channels:
  - defaults
show_channel_urls: true
default_channels:
  - {TSINGHUA_CONDA_MAIN}
  - {TSINGHUA_CONDA_R}
custom_channels:
  conda-forge: {TSINGHUA_CONDA_CLOUD}
"""

_MINICONDA_URL = re.compile(r"https://repo\.anaconda\.com/miniconda/")
_APT_UPDATE = re.compile(r"^RUN apt update && apt install -y", re.MULTILINE)
_CONDA_INIT = re.compile(r"^RUN conda init --all$", re.MULTILINE)


class RecipeError(RuntimeError):
    """官方脚本长得和预期不一样，改写没落到实处。宁可报错也不要建出一个半改的镜像。"""


@dataclass(frozen=True, slots=True)
class BuildSpecs:
    """`datasets/swebench/build-specs.json` 读进来的样子。"""

    swebench_version: str
    base_dockerfile: str
    #: env key → {env_dockerfile, setup_env_script}
    envs: Mapping[str, Mapping[str, str]]
    #: instance_id → {env_image_key, instance_dockerfile, install_repo_script, eval_script}
    instances: Mapping[str, Mapping[str, str]]


def load_build_specs(path: Path) -> BuildSpecs:
    data = json.loads(path.read_text(encoding="utf-8"))
    return BuildSpecs(
        swebench_version=str(data["swebench_version"]),
        base_dockerfile=str(data["base_dockerfile"]),
        envs=data["envs"],
        instances=data["instances"],
    )


# ══════════════════════════════════════════════════════════════
# 1. base
# ══════════════════════════════════════════════════════════════


def rewrite_base_dockerfile(official: str) -> str:
    """官方 base Dockerfile → 走清华源的版本。三处替换都必须命中，否则抛错。"""
    if not _MINICONDA_URL.search(official):
        raise RecipeError("官方 base Dockerfile 里没找到 repo.anaconda.com 的 miniconda 下载行")
    if not _APT_UPDATE.search(official):
        raise RecipeError("官方 base Dockerfile 里没找到 `RUN apt update && apt install -y`")
    if not _CONDA_INIT.search(official):
        raise RecipeError("官方 base Dockerfile 里没找到 `RUN conda init --all`")

    text = _MINICONDA_URL.sub(TSINGHUA_MINICONDA + "/", official)
    # apt 源换清华：ubuntu:22.04 的源写在 /etc/apt/sources.list
    text = _APT_UPDATE.sub(
        "RUN sed -i "
        f"'s|http://archive.ubuntu.com|{TSINGHUA_APT}|g; "
        f"s|http://security.ubuntu.com|{TSINGHUA_APT}|g' /etc/apt/sources.list\n"
        "RUN apt update && apt install -y",
        text,
        count=1,
    )
    # conda 源 + pip 源。放在 conda init 之前：init 之后官方还会 append conda-forge，
    # 那句要落到 .condarc 的 custom_channels 上，所以 .condarc 得先写好
    condarc_lines = " \\\n".join(
        f"    && echo '{line}' >> /root/.condarc" for line in CONDARC.splitlines()
    )
    text = _CONDA_INIT.sub(
        "RUN rm -f /root/.condarc \\\n"
        + condarc_lines
        + f"\nENV PIP_INDEX_URL={TSINGHUA_PYPI}\n"
        + "RUN conda init --all",
        text,
        count=1,
    )
    return text


# ══════════════════════════════════════════════════════════════
# 2. env
# ══════════════════════════════════════════════════════════════

_FROM_LINE = re.compile(r"^FROM .*$", re.MULTILINE)


def rewrite_env_dockerfile(official: str) -> str:
    """只换 FROM。脚本里 conda / pip 已经通过 base 的 .condarc 和 PIP_INDEX_URL 走清华。"""
    if not _FROM_LINE.search(official):
        raise RecipeError("官方 env Dockerfile 没有 FROM 行")
    return _FROM_LINE.sub(f"FROM {BASE_TAG}", official, count=1)


# ══════════════════════════════════════════════════════════════
# 3. instance
# ══════════════════════════════════════════════════════════════

_GIT_CLONE = re.compile(r"^git clone -o origin https://github\.com/\S+ /testbed$", re.MULTILINE)
_GIT_RESET = re.compile(r"^git reset --hard [0-9a-f]{40}$", re.MULTILINE)
_GIT_REMOTE_REMOVE = re.compile(r"^git remote remove origin$", re.MULTILINE)
_QHULL_WGET = re.compile(r'^wget -O "\$QHULL_TAR" "\$QHULL_URL"$', re.MULTILINE)

#: 官方脚本里 clone 那一行换成这一段：解 tar、建一个只有一个提交的 git 仓库。
#: `git add -A` 尊重仓库自己的 .gitignore，所以之后 `git ls-files --ignored` 认得出构建产物
#: （`pre_test_command` 靠它拷 `.so`）。
#:
#: 官方镜像是 2024 年建的，当时 pip 24 / setuptools 69；现在从清华源装到的是 pip 26 /
#: setuptools 80+，两处对不上（2026-09-16 实测，43 道里挂了 2 道）：
#:   - `pip install --no-use-pep517` 这个开关 pip 25 删了（scikit-learn-25102）；
#:   - 隔离构建环境里装的最新 setuptools 没有 `pkg_resources` 了（astropy-8707 的 setup.py 要它）。
#: 前者把开关去掉，后者用 PIP_CONSTRAINT 把隔离构建环境里的 setuptools 按回当年那一代。
#: 只在 setup_repo.sh 里生效，不进运行时。
#: 约束按"官方镜像那一代（2024 年中）最后一个大版本"取：setuptools 70 之后隔离构建里没有
#: pkg_resources；docutils 0.22 去掉了 `docutils.utils.roman`，sphinx 3.x/4.x 的 latex writer
#: 一导入就炸（tests 里 `app` fixture 会加载 latex builder，5 道 sphinx 题因此在 S4 报 ERROR）。
PIP_CONSTRAINTS = ("setuptools<70", "docutils<0.22")
_PIN_BUILD_DEPS = (
    "printf '%s\\n' "
    + " ".join(f"'{c}'" for c in PIP_CONSTRAINTS)
    + " > /root/pip-constraints.txt\n"
    "export PIP_CONSTRAINT=/root/pip-constraints.txt\n"
)
_NO_USE_PEP517 = re.compile(r" --no-use-pep517\b")

_UNPACK_REPO = """\
mkdir -p /testbed
tar -xf /root/repo.tar -C /testbed
cd /testbed
git init -q
git add -A
git -c user.name=bench -c user.email=bench@localhost commit -q -m base"""

#: qhull 源码包（matplotlib 3.x 的 pre_install 要它）先下到构建上下文里，脚本里改成拷贝。
QHULL_TARBALL = "qhull-2020-src-8.0.2.tgz"
QHULL_URL = f"http://www.qhull.org/download/{QHULL_TARBALL}"

#: matplotlib 的 `setup.py` 构建时自己去下的 FreeType（测试基线图是用 2.6.1 渲染的，不能换系统的）。
#: 它下载前先查 `~/.cache/matplotlib/<sha256>`，命中就不联网 —— 我们把包放到这个位置。
#: sha256 是 `setupext.py` 里 `_freetype_hashes['2.6.1']`，样本里 3.4–3.7 每个提交都是这个值。
FREETYPE_TARBALL = "freetype-2.6.1.tar.gz"
FREETYPE_SHA256 = "0a3c7dfbda6da1e8fce29232e8e96d987ababbbf71ebc8c75659e4132c367014"
FREETYPE_URLS = (
    f"https://download.savannah.gnu.org/releases/freetype/freetype-old/{FREETYPE_TARBALL}",
    f"https://download.savannah.gnu.org/releases/freetype/{FREETYPE_TARBALL}",
    f"https://downloads.sourceforge.net/project/freetype/freetype2/2.6.1/{FREETYPE_TARBALL}",
)
FREETYPE_CACHE_PATH = f"/root/.cache/matplotlib/{FREETYPE_SHA256}"
_MATPLOTLIB_CLONE = "https://github.com/matplotlib/matplotlib /testbed"

#: 没有 `setup.py` 的仓库，`pip install -e .` 之前把 pip 按回官方那一代（见模块文档第 6 条）。
LEGACY_EDITABLE_PIP = "pip<25"
_CONDA_ACTIVATE = re.compile(r"^conda activate testbed$", re.MULTILINE)
_LEGACY_PIP_LINE = f"python -m pip install '{LEGACY_EDITABLE_PIP}'"


def rewrite_repo_script(official: str, *, legacy_pip: bool = False) -> str:
    """官方 `setup_repo.sh` → 不联 GitHub 的版本。

    `legacy_pip=True`（仓库没有 `setup.py`）时，在第一句 `conda activate testbed` 之后把 pip
    按回 24，让 pyproject-only 仓库的 editable 安装还能走 `setup.py develop` 那条退路。
    """
    if not _GIT_CLONE.search(official):
        raise RecipeError(
            "官方 setup_repo.sh 里没找到 `git clone -o origin https://github.com/... /testbed`"
        )
    if not _GIT_RESET.search(official) or not _GIT_REMOTE_REMOVE.search(official):
        raise RecipeError(
            "官方 setup_repo.sh 里没找到 `git reset --hard` / `git remote remove origin`"
        )

    # 用函数做替换：替换串里的 `\n` 不能让 re 当转义解释
    text = _GIT_CLONE.sub(lambda _: _PIN_BUILD_DEPS + _UNPACK_REPO, official, count=1)
    text = _NO_USE_PEP517.sub("", text)
    text = _GIT_RESET.sub("", text, count=1)
    text = _GIT_REMOTE_REMOVE.sub("", text, count=1)
    if legacy_pip:
        if not _CONDA_ACTIVATE.search(text):
            raise RecipeError(
                "官方 setup_repo.sh 里没找到 `conda activate testbed`，pip 按回的位置定不了"
            )
        text = _CONDA_ACTIVATE.sub(
            lambda _: f"conda activate testbed\n{_LEGACY_PIP_LINE}", text, count=1
        )
    if "QHULL_URL" in text:
        if not _QHULL_WGET.search(text):
            raise RecipeError("脚本里有 QHULL_URL 但没找到预期的 wget 行，改写方式要重新看")
        text = _QHULL_WGET.sub(f'cp /root/{QHULL_TARBALL} "$QHULL_TAR"', text, count=1)
    # 连续空行压掉，看着清爽，也不影响 bash
    return re.sub(r"\n{3,}", "\n\n", text)


def needs_qhull(official_repo_script: str) -> bool:
    return "QHULL_URL" in official_repo_script


def needs_freetype(official_repo_script: str) -> bool:
    """matplotlib 的题都要：`setup.py` 编译时下 FreeType 2.6.1（不分版本，3.4–3.7 都是）。"""
    return _MATPLOTLIB_CLONE in official_repo_script


def instance_dockerfile(
    env_image_key: str, *, version: str, with_qhull: bool, with_freetype: bool = False
) -> str:
    """instance 的 Dockerfile 自己写，不改官方那份：官方的只有 COPY 一个脚本，我们多两样东西。

    `SETUPTOOLS_SCM_PRETEND_VERSION`：官方镜像里 `/testbed` 带 git tag，setuptools_scm 能算出
    版本号；我们只有一个没有 tag 的提交，算出来是 `0.1.dev1`。用数据集里的 `version` 字段
    顶上（matplotlib `3.7`、astropy `5.1`），不用 setuptools_scm 的包不受影响。
    """
    lines = [
        f"FROM {env_tag(env_image_key)}",
        "",
        "COPY ./setup_repo.sh /root/",
        "COPY ./repo.tar /root/repo.tar",
    ]
    if with_qhull:
        lines.append(f"COPY ./{QHULL_TARBALL} /root/{QHULL_TARBALL}")
    if with_freetype:
        # 放进 matplotlib 自己的下载缓存，setup.py 见到就不联网了
        lines.append(f"COPY ./{FREETYPE_TARBALL} {FREETYPE_CACHE_PATH}")
    lines += [
        f"ENV SETUPTOOLS_SCM_PRETEND_VERSION={version}",
        "RUN sed -i -e 's/\\r$//' /root/setup_repo.sh",
        "RUN /bin/bash /root/setup_repo.sh",
        "RUN rm -f /root/repo.tar",
        "",
        "WORKDIR /testbed/",
        "",
    ]
    return "\n".join(lines)


def build_context_files(
    specs: BuildSpecs, instance_id: str, *, version: str, legacy_pip: bool = False
) -> dict[str, str]:
    """一道题的 instance 构建上下文里要写的文本文件：`{文件名: 内容}`。

    `repo.tar`、qhull、FreeType 三个二进制另放。`legacy_pip` 由调用方看仓库有没有 `setup.py` 决定。
    """
    spec = specs.instances[instance_id]
    script = spec["install_repo_script"]
    return {
        "Dockerfile": instance_dockerfile(
            spec["env_image_key"],
            version=version,
            with_qhull=needs_qhull(script),
            with_freetype=needs_freetype(script),
        ),
        "setup_repo.sh": rewrite_repo_script(script, legacy_pip=legacy_pip),
    }


_YML_CONDA_FORGE_ONLY = re.compile(r"^channels:\n  - conda-forge\n(?!  - nodefaults)", re.MULTILINE)


def rewrite_env_script(official: str) -> str:
    """官方 `setup_env.sh` 基本原样，只改一处：`environment.yml` 的 channels 加 `nodefaults`。

    matplotlib 的 3 个环境是 `conda env create` 出来的，yml 只写了 conda-forge，而 .condarc
    里还有 defaults，两个大频道混着解，libmamba 跑 58 分钟、吃 7.6 GB 还没解出来；
    只留 conda-forge 几分钟就解完（2026-09-16 实测）。yml 里 conda-forge 排第一，包本来
    就优先从它取，加 nodefaults 对解出来的东西影响很小。
    """
    return _YML_CONDA_FORGE_ONLY.sub("channels:\n  - conda-forge\n  - nodefaults\n", official)


def env_context_files(specs: BuildSpecs, env_image_key: str) -> dict[str, str]:
    spec = specs.envs[env_image_key]
    return {
        "Dockerfile": rewrite_env_dockerfile(spec["env_dockerfile"]),
        "setup_env.sh": rewrite_env_script(spec["setup_env_script"]),
    }


def base_context_files(specs: BuildSpecs) -> dict[str, str]:
    return {"Dockerfile": rewrite_base_dockerfile(specs.base_dockerfile)}


def summarize(specs: BuildSpecs) -> dict[str, Any]:
    """给报告用：几个环境、几道题、哪些要 qhull。"""
    return {
        "swebench_version": specs.swebench_version,
        "envs": len(specs.envs),
        "instances": len(specs.instances),
        "need_qhull": sorted(
            i for i, s in specs.instances.items() if needs_qhull(s["install_repo_script"])
        ),
        "need_freetype": sorted(
            i for i, s in specs.instances.items() if needs_freetype(s["install_repo_script"])
        ),
    }


__all__ = [
    "BASE_TAG",
    "CONDARC",
    "FREETYPE_CACHE_PATH",
    "FREETYPE_SHA256",
    "FREETYPE_TARBALL",
    "FREETYPE_URLS",
    "LEGACY_EDITABLE_PIP",
    "MIRROR_HOSTS",
    "QHULL_TARBALL",
    "QHULL_URL",
    "BuildSpecs",
    "RecipeError",
    "base_context_files",
    "build_context_files",
    "env_context_files",
    "env_tag",
    "instance_dockerfile",
    "instance_tag",
    "load_build_specs",
    "needs_freetype",
    "needs_qhull",
    "rewrite_base_dockerfile",
    "rewrite_env_dockerfile",
    "rewrite_repo_script",
    "summarize",
]
