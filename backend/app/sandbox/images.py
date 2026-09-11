"""镜像分层与构建器（E2-T3，`05-sandbox.md` §10.4、ADR-008）。

预建镜像不是优化，是 MET-02（300 次评测跑进 6 小时）的**必要条件**：每次评测现装依赖
要多花 60–180 秒，300 次就是 5–15 小时，单这一项就把预算撑爆了（§18.2 那张表）。
所以依赖只在建镜像时装一次，评测时容器起来就能跑。

## 三层

    bench-base:py311                 OS + 编译工具链 + git + pytest + uv      1 个
      └─ bench-env:{environment_id}  某个仓库的依赖 + 快照 + 依赖锁          每环境 1 个
           └─ bench-agent:{env}-{agent}  再加被测 AI 的 CLI                 env × agent

第一层的 Dockerfile 在 `images/base/`，手写。第二层由 `render_env_dockerfile()` 从
`images/envs/{environment_id}.json` 渲染出来 —— 9 个仓库最多 18 个环境，彼此只差
四五个变量，写 18 份 Dockerfile 等于改一处公共逻辑要改 18 遍。第三层复用
`images/{aider,claude-code}/Dockerfile`，只把 `BASE_IMAGE` 这个构建参数换掉。

## 这个模块不碰数据库，也不碰制品存储

它只负责"建出来、验一遍、把事实带回去"，返回一个 `BuildOutcome`。写
`environment_specs`、把构建日志落成制品，都在 `cli/images.py` 里做。

两个理由：一是模块边界（sandbox 层长出业务逻辑之后就没法单独测了）；二是这里
绝大多数逻辑（配方解析、Dockerfile 渲染、配方哈希、磁盘水位、回收名单）都是纯函数，
不挂数据库才能进 `make test` 那一批。

## 建完必须自查，不通过就让构建失败

env 镜像里躺着一份仓库快照（`/opt/repo`，装依赖要用它），而评测时挂进来的工作区
（`/workspace`）是另一个 commit、还被被测 AI 改过。要是 `import sqlfluff` 解析到了
镜像里那一份，**被测 AI 的改动根本不会被执行** —— 测试照跑、可能还全绿，
而 Oracle 哨兵会从 100% 悄悄掉下去，日志里一点异常都没有。

`smoke_check()` 把这件事变成建镜像时的一次断言。详见它的注释。

## 几个来自实测的硬约束

- **dockerd 会把代理注进每个构建步骤**（2026-09-08 实测，走 SDK 触发也一样）。
  走代理拉 `deb.debian.org` 是 9.2 秒一个请求、直连 1.4 秒，所以 apt 步骤要
  显式 `env -u http_proxy ...` 绕开它。
- **pip 必须走国内镜像源**，直连 PyPI 会大面积超时，表现成"这个包装不上"。
- **`HOME` 和 `TMPDIR` 都不能留在容器的 `/tmp`**，那是 tmpfs、吃内存额度。
  这一条在 `images/base/Dockerfile` 里解决，见那里的注释。

（以上三条是 `03-benchmark-spec.md` §8.8 那张表的第 ①③④⑤ 条。）
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docker.errors import APIError, DockerException, ImageNotFound

from app.infrastructure.logging import get_logger
from app.sandbox.container import (
    BENCH_LABEL,
    BENCH_LABEL_VALUE,
    WORKSPACE_TARGET,
    BindMount,
    ContainerSpec,
    ImageInfo,
    NetworkMode,
    ResourceLimits,
    SandboxError,
    Stage,
    build_env,
    digest_reference,
    get_docker_client,
    inspect_image,
    run_in_container,
)
from app.sandbox.workspace import materialize_workspace

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

#: 第一层的标签。
BASE_TAG = "bench-base:py311"

#: 第一层的底座，**按 digest 引用**（协议 C-36 对 FROM 一样成立）。
#: 和 `images/base/Dockerfile` 里的 FROM 行必须一致 —— 换基础镜像时两处一起改，
#: `test_base_dockerfile_pins_the_same_digest` 会盯着这一点。
BASE_IMAGE_REF = "python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"

#: 第二、三层的标签前缀。冒号后面填 `environment_id` / `{env}-{agent}`。
ENV_TAG_PREFIX = "bench-env"
AGENT_TAG_PREFIX = "bench-agent"

#: 镜像上打的标签（label）。回收（`gc`）和缓存判定都靠它们，不靠镜像名字 ——
#: 名字是我们编的字符串，label 是 daemon 侧的结构化字段，能直接 filter。
LABEL_LAYER = "bench.layer"
LABEL_ENVIRONMENT_ID = "bench.environment_id"
LABEL_AGENT = "bench.agent"
LABEL_RECIPE_HASH = "bench.recipe_hash"

#: `bench.layer` 的三个取值。
LAYER_BASE = "base"
LAYER_ENV = "env"
LAYER_AGENT = "agent"

#: 配方哈希的算法版本。改了渲染逻辑或哈希口径就把它加一 ——
#: 不加的话，旧镜像的 label 仍然"命中"，于是改完代码重建，什么都没发生。
RECIPE_HASH_VERSION = "1"

#: pip 的国内镜像源。**不是可选项**：这台机器直连 PyPI 会大面积超时
#: （`ReadTimeoutError` / `SSL: UNEXPECTED_EOF`，§8.8 坑 ④，风险 R18）。
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

#: apt 的国内镜像源。
DEFAULT_APT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/debian"

#: 镜像里放依赖锁和其他构建产物的地方。
BENCH_DIR = "/opt/bench"
#: 仓库快照在镜像里的位置。**不在 `/workspace`**：那个位置留给评测时挂进来的工作区。
REPO_DIR = "/opt/repo"
#: 依赖锁在镜像里的路径。`pip freeze` 的结果，建完会被读出来存成制品。
LOCK_PATH = f"{BENCH_DIR}/requirements.lock"

#: 前插 `sys.path` 的那个 `.pth` 文件名。
#:
#: `zzz-` 前缀不是随便起的：`site` 模块按文件名字典序处理 `.pth`，
#: 排在 pip 的 editable 安装那几个后面，我们插的路径才在最前面。
WORKSPACE_PTH_NAME = "zzz-bench-workspace.pth"

#: 工作区里默认去哪几个目录找包。`""` 表示工作区根目录本身。
#: `src` 在前：src 布局的仓库包在 `src/<pkg>`，工作区根底下没有那个名字。
DEFAULT_SOURCE_ROOTS: tuple[str, ...] = ("src", "")

#: 构建日志留多少字节。pip 装 torch 那一套能刷出几 MB，全留着制品会很难看，
#: 全丢掉又查不了问题 —— 掐头留尾，中间标一行省略了多少。
MAX_BUILD_LOG_BYTES = 8 * 1024 * 1024

#: 自查容器的限额。只做 import 和 collect，不需要多少资源；
#: 内存给 2 GiB 是因为有些仓库光 import 就要拉起一堆重模块。
SMOKE_LIMITS = ResourceLimits(cpus=2.0, memory_mb=2048, pids_limit=512)
#: 自查容器的墙钟上限（秒）。
SMOKE_TIMEOUT_S = 300

#: 磁盘剩余低于这个比例就拒绝开建（§10.4「镜像治理」的水位监控）。
DEFAULT_MIN_FREE_RATIO = 0.15

#: 合法的镜像 tag 片段。docker 只认这些字符，环境 id 里混进 `/` 或者中文都建不出来。
_TAG_PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,110}$")

#: 40 位十六进制的 commit sha。
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


# ══════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════


class ImageBuildError(SandboxError):
    """构建失败。消息里带上构建日志的最后几行。"""


class RecipeError(SandboxError):
    """配方文件有问题（字段缺失、取值非法）。"""


class DiskSpaceError(SandboxError):
    """磁盘剩余不够，拒绝开建。"""


class SmokeCheckError(SandboxError):
    """镜像建出来了，但自查没过。

    异常里**带着这次构建的 `BuildOutcome`**。自查失败是最需要看构建日志和
    依赖锁的时候（"到底装了什么才导致 import 落到了快照上"），而那时候镜像还没被
    调用方接受 —— 不把事实一起抛出来，调用方就只剩一句报错，日志全丢了。
    """

    def __init__(self, message: str, *, outcome: BuildOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


# ══════════════════════════════════════════════════════════════
# 配方
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class EnvRecipe:
    """一个环境镜像的配方，对应 `images/envs/{environment_id}.json` 一份文件。

    为什么单独存一份文件、不从 `environment_specs` 表里读：真实仓库的环境规格要等
    E1-T4 挖掘器跑出来才有，而镜像得先建好才有得跑（题目验证流水线 S3 拿不到镜像
    就一律判 `ENV_UNBUILDABLE`）。配方进版本库，几百字节，也让"这个仓库当初是
    怎么装上的"有据可查。

    表里有对应行时，构建器会把 tag / digest / 状态回填进去；没有也照建不误。
    """

    #: 和 `environment_specs.environment_id` 同一个值，也是镜像 tag 的后半截。
    environment_id: str
    #: `owner/repo`。
    repo_name: str
    #: clone 用的地址。Golden 题用 `golden://` 开头的假地址，镜像已经在本地。
    repo_url: str
    #: 装依赖用哪个 commit 的快照。**不是**题目的 `base_commit` ——
    #: 一个环境要覆盖同一仓库的一批题，快照取的是"依赖关系稳定的那个点"。
    snapshot_commit: str
    python_version: str = "3.11"

    #: 这个仓库比别人多要的系统包。基础镜像已经有编译工具链，这里只写仓库特有的
    #: （libpq-dev、libxml2-dev 之类）。写在配方里而不是塞进 bench-base，
    #: 是为了让"谁比别人多要什么"看得见。
    apt_packages: tuple[str, ...] = ()

    #: 装依赖的命令，按顺序跑，**每条都必须成功**。
    #:
    #: 故意不给默认值：一个仓库怎么装是它自己的事实（有的要 `--no-build-isolation`，
    #: 有的要 uv，有的压根不用装），把它藏进 Python 常量里，出问题时没人查得到
    #: 当初到底跑了什么。空列表是合法的 —— Golden 题就不用装。
    install_steps: tuple[str, ...] = ()

    #: 工作区里去哪几个目录找包，会被写进 `.pth` 前插到 `sys.path`。
    workspace_source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS

    #: 自查要 import 的模块名。**建完当场验它们解析到 `/workspace` 底下**。
    #: 空列表等于放弃这道检查，所以配方里应当写上，评审时也该盯着这一项。
    import_check: tuple[str, ...] = ()

    #: 自查时跑一遍 `pytest --collect-only`，确认用例收得到。
    #: 少数仓库的全量收集本来就带错误（可选依赖没装），那种情况下显式关掉并写明原因。
    collect_check: bool = True

    #: 跑 pytest 时额外带的参数，**应当照抄仓库自己的测试命令**。
    #:
    #: 为空就是"从工作区根收集全部"，多数仓库这样是对的。但有的仓库不行，
    #: 而且失败方式很有误导性 —— LLaMA-Factory 的 `scripts/api_example/` 底下有两个
    #: 叫 `test_*.py` 的文件，那是 API 用法示例不是测试，收集时 import 一个没装的
    #: `openai` 就报错；它还有两个同名的 `test_converter.py`，默认导入模式下撞车。
    #: 而仓库自己跑的是 `pytest --import-mode=importlib tests/ tests_v1/`，一个错都没有
    #: （2026-09-08 实测：默认口径 348 条 + 3 个错，仓库口径 359 条 + 0 个错）。
    #:
    #: 换句话说，**"这个环境能不能跑测试"要按仓库自己的口径问**，按我们编的口径问
    #: 会得到一个吓人但没意义的答案。这个字段之后会变成 `environment_specs.test_command`
    #: 的一部分（E8-T2），现在先在自查里用上，等于提前验了一遍。
    test_args: tuple[str, ...] = ()

    #: 关掉自查的理由。`import_check` 为空或 `collect_check=False` 时必须写，
    #: 否则加载配方直接报错 —— 不写理由的豁免，三周后没人知道当初为什么放行。
    checks_waived_because: str | None = None

    pip_index_url: str = DEFAULT_PIP_INDEX_URL
    apt_mirror: str = DEFAULT_APT_MIRROR

    #: 备注，只进配方文件和构建证据，不影响构建结果。
    note: str | None = None

    def __post_init__(self) -> None:
        if not _TAG_PART_RE.match(self.environment_id):
            raise RecipeError(
                f"environment_id {self.environment_id!r} 不能直接当镜像 tag 用："
                "只允许字母数字和 _ . -，且不能以 . 或 - 开头"
            )
        if "/" not in self.repo_name:
            raise RecipeError(f"repo_name 要写成 owner/repo，收到 {self.repo_name!r}")
        if not _COMMIT_RE.match(self.snapshot_commit):
            raise RecipeError(
                f"snapshot_commit 要是 40 位小写十六进制，收到 {self.snapshot_commit!r}"
            )
        waived = not self.import_check or not self.collect_check
        if waived and not self.checks_waived_because:
            raise RecipeError(
                f"{self.environment_id}：关掉了自查（import_check 为空或 collect_check=False），"
                "必须在 checks_waived_because 里写明理由"
            )

    @property
    def image_tag(self) -> str:
        """这个环境的镜像 tag。"""
        return f"{ENV_TAG_PREFIX}:{self.environment_id}"

    def canonical(self) -> dict[str, Any]:
        """算配方哈希用的规范形式。字段顺序固定，不受 JSON 文件里的书写顺序影响。

        `note` 不进来：它是给人看的备注，改一句话不该让所有镜像重建。
        """
        return {
            "environment_id": self.environment_id,
            "repo_name": self.repo_name,
            "snapshot_commit": self.snapshot_commit,
            "python_version": self.python_version,
            "apt_packages": list(self.apt_packages),
            "install_steps": list(self.install_steps),
            "workspace_source_roots": list(self.workspace_source_roots),
            "import_check": list(self.import_check),
            "collect_check": self.collect_check,
            "test_args": list(self.test_args),
            "pip_index_url": self.pip_index_url,
            "apt_mirror": self.apt_mirror,
        }


#: 配方文件里认得的字段。多写一个字段直接报错，不是悄悄忽略 ——
#: 悄悄忽略的话，把 `install_steps` 拼成 `install_step` 会表现成"什么都没装"。
_RECIPE_FIELDS = frozenset(
    {
        "environment_id",
        "repo_name",
        "repo_url",
        "snapshot_commit",
        "python_version",
        "apt_packages",
        "install_steps",
        "workspace_source_roots",
        "import_check",
        "collect_check",
        "test_args",
        "checks_waived_because",
        "pip_index_url",
        "apt_mirror",
        "note",
    }
)

_TUPLE_FIELDS = (
    "apt_packages",
    "install_steps",
    "workspace_source_roots",
    "import_check",
    "test_args",
)


def parse_recipe(data: Mapping[str, Any], *, source: str = "<内存>") -> EnvRecipe:
    """把一份 JSON 变成 `EnvRecipe`。字段不认识、类型不对都当场报错。"""
    unknown = set(data) - _RECIPE_FIELDS
    if unknown:
        raise RecipeError(f"{source}：不认识的字段 {sorted(unknown)}")
    missing = {"environment_id", "repo_name", "repo_url", "snapshot_commit"} - set(data)
    if missing:
        raise RecipeError(f"{source}：缺少必填字段 {sorted(missing)}")

    kwargs: dict[str, Any] = dict(data)
    for name in _TUPLE_FIELDS:
        if name in kwargs:
            value = kwargs[name]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise RecipeError(f"{source}：{name} 要是字符串列表，收到 {value!r}")
            kwargs[name] = tuple(value)
    return EnvRecipe(**kwargs)


def load_recipe(path: Path) -> EnvRecipe:
    """读一份配方文件。文件名（去掉 `.json`）必须和里面的 `environment_id` 一致。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeError(f"读不了配方 {path}：{exc}") from exc
    recipe = parse_recipe(data, source=str(path))
    if path.stem != recipe.environment_id:
        raise RecipeError(
            f"{path}：文件名和 environment_id 对不上（{path.stem} vs {recipe.environment_id}）。"
            "对不上就没法从环境 id 直接找到配方"
        )
    return recipe


def load_recipes(directory: Path) -> list[EnvRecipe]:
    """读一个目录下所有配方，按 `environment_id` 排序返回。"""
    return sorted(
        (load_recipe(p) for p in sorted(Path(directory).glob("*.json"))),
        key=lambda r: r.environment_id,
    )


# ══════════════════════════════════════════════════════════════
# Dockerfile 渲染
# ══════════════════════════════════════════════════════════════

#: apt 步骤前面这一串。dockerd 把它自己的代理注进了每个构建步骤，而走代理拉
#: deb.debian.org 是 9.2 秒一个请求、直连 1.4 秒（2026-09-08 实测）。
_NO_PROXY_PREFIX = "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY"

#: 问 Python 自己 site-packages 在哪的那段 shell。
#:
#: 提成常量是因为它嵌了三层引号（Python 字符串 → shell 双引号 → shell 单引号 →
#: Python 里的双引号），塞在 Dockerfile 的行列表里再拆行，很容易把生成出来的命令
#: 改坏而单元测试还照过 —— 渲染结果是字符串比对，坏了才发现。
#:
#: 不写死 `/usr/local/lib/python3.11/site-packages`：换 Python 小版本这个路径就变了，
#: 而写死的话不会报错，只会让 `.pth` 落在一个没人读的目录里。
_PURELIB_SHELL = (
    'PURELIB="$(python -c \'import sysconfig; print(sysconfig.get_paths()["purelib"])\')"'
)


def workspace_pth_line(source_roots: Sequence[str]) -> str:
    """生成 `.pth` 文件里那一行。

    `.pth` 文件里以 `import` 开头的行会被 `site` 模块直接执行 —— 这是标准行为，
    不是奇技淫巧，pip 的 editable 安装用的也是它。

    `reversed()` 不能省：`insert(0, p)` 是逐个插到最前面，按原顺序插完之后
    最后一个反而排在最前。倒着插，最终顺序才和配方里写的一致。

    `isdir` 判断是为了让同一个镜像在"没挂工作区"的时候也能正常起 ——
    比如 Agent 阶段的容器、或者手工 `docker run` 进去看看。
    """
    paths = [f"{WORKSPACE_TARGET}/{r}".rstrip("/") if r else WORKSPACE_TARGET for r in source_roots]
    literal = ", ".join(json.dumps(p) for p in paths)
    return (
        "import sys, os; "
        f"[sys.path.insert(0, p) for p in reversed([{literal}]) if os.path.isdir(p)]"
    )


def render_env_dockerfile(recipe: EnvRecipe, *, base_tag: str = BASE_TAG) -> str:
    """把配方渲染成一份 Dockerfile。

    输出是确定性的：同一份配方永远渲染出同一段文本，因为它要参与配方哈希，
    而配方哈希决定"这次要不要重建"。
    """
    lines: list[str] = [
        f"# 由 app/sandbox/images.py 从 images/envs/{recipe.environment_id}.json 渲染。",
        "# 不要手改：下次 `bench images build` 会原样覆盖。",
        f"FROM {base_tag}",
        "",
        f"ARG PIP_INDEX_URL={recipe.pip_index_url}",
        "ENV PIP_INDEX_URL=${PIP_INDEX_URL}",
        "",
    ]

    if recipe.apt_packages:
        packages = " ".join(sorted(recipe.apt_packages))
        lines += [
            "# 这个仓库特有的系统包。绕开代理，理由见 bench-base 的同名步骤。",
            f"ARG APT_MIRROR={recipe.apt_mirror}",
            'RUN sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" '
            "/etc/apt/sources.list.d/debian.sources \\",
            f"    && {_NO_PROXY_PREFIX} apt-get update \\",
            f"    && {_NO_PROXY_PREFIX} apt-get install -y --no-install-recommends {packages} \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
        ]

    lines += [
        "# 仓库快照。装依赖要读它的 pyproject.toml / setup.py；评测时真正被测的代码",
        f"# 是挂在 {WORKSPACE_TARGET} 的工作区，不是这一份。",
        f"COPY repo {REPO_DIR}",
        f"WORKDIR {REPO_DIR}",
        "",
    ]

    if recipe.install_steps:
        lines.append("# 装依赖。每条都必须成功 —— 装了一半的环境比装不上更难查。")
        lines += [f"RUN {step}" for step in recipe.install_steps]
        lines.append("")

    pth_line = workspace_pth_line(recipe.workspace_source_roots)
    lines += [
        "# 把工作区的源码目录前插进 sys.path。",
        "#",
        "# 没有这一步，src 布局的仓库会 import 到上面那份快照，于是被测 AI 改的代码",
        "# 根本不会被执行 —— 测试照跑、可能全绿，Oracle 哨兵却会莫名其妙掉下去。",
        "# 建完由 smoke_check() 当场验一遍，验不过这次构建就算失败。",
        f"RUN {_PURELIB_SHELL} \\",
        f"    && printf '%s\\n' '{pth_line}' > \"$PURELIB/{WORKSPACE_PTH_NAME}\" \\",
        f'    && cat "$PURELIB/{WORKSPACE_PTH_NAME}"',
        "",
        "# 依赖锁（§10.4 说的 pip freeze lock）。建完会被读出来存成制品，",
        "# 两次构建的锁一 diff 就知道依赖漂到哪去了。",
        f"RUN python -m pip freeze --all > {LOCK_PATH} \\",
        '    && python -c "import pytest; print(pytest.__version__)"'
        f" > {BENCH_DIR}/pytest-version.txt",
        "",
        f"WORKDIR {WORKSPACE_TARGET}",
        "",
    ]
    return "\n".join(lines)


def compute_recipe_hash(
    *, dockerfile: str, base_ref: str, snapshot_tree_sha: str, recipe: EnvRecipe
) -> str:
    """算配方哈希，写进镜像的 `bench.recipe_hash` 标签。

    重跑 `build` 时先比这个标签，一样就整个跳过 —— 这是最强的一层缓存，
    比 docker 自己的层缓存还早一步（连构建上下文都不用打包）。

    四个输入缺一不可：
    - `dockerfile`：渲染结果变了当然要重建
    - `base_ref`：底座换了，上面装的依赖解析结果可能整个不一样
    - `snapshot_tree_sha`：仓库快照的内容变了（配方里换了 commit）
    - `recipe`：自查项之类不进 Dockerfile、但会改变"这次构建算不算数"的字段
    """
    payload = json.dumps(
        {
            "version": RECIPE_HASH_VERSION,
            "base_ref": base_ref,
            "dockerfile": dockerfile,
            "snapshot_tree_sha": snapshot_tree_sha,
            "recipe": recipe.canonical(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════
# 磁盘水位
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DiskHeadroom:
    """镜像存储所在分区的余量。"""

    path: Path
    total_bytes: int
    free_bytes: int

    @property
    def free_ratio(self) -> float:
        return self.free_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def free_gib(self) -> float:
        return self.free_bytes / 2**30

    def describe(self) -> str:
        return (
            f"{self.path}：剩 {self.free_gib:.1f} GiB / "
            f"{self.total_bytes / 2**30:.1f} GiB（{self.free_ratio:.1%}）"
        )


def docker_root_dir(client: Any = None) -> Path:
    """镜像和容器层存在哪个目录。取不到就退回 `/var/lib/docker`。

    要问 daemon 而不是写死：这台机器上同时装了原生 dockerd 和 Docker Desktop，
    两边的存储目录不一样，量错分区等于水位检查完全失效（AGENTS.md 第 10 节）。
    """
    docker_client = client or get_docker_client()
    try:
        info = docker_client.info()
    except (DockerException, OSError) as exc:  # pragma: no cover - 连不上 daemon 时
        logger.warning("拿不到 docker info，磁盘水位按 /var/lib/docker 量", error=str(exc))
        return Path("/var/lib/docker")
    return Path(str(info.get("DockerRootDir") or "/var/lib/docker"))


def disk_headroom(path: Path) -> DiskHeadroom:
    """量一个路径所在分区的余量。

    路径不存在就往上找最近的存在的祖先 —— `/var/lib/docker` 在某些部署下
    普通用户 stat 不了，直接抛异常的话水位检查就变成了"永远拦不住"。
    """
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return DiskHeadroom(path=Path(path), total_bytes=usage.total, free_bytes=usage.free)


def require_disk(headroom: DiskHeadroom, *, min_free_ratio: float = DEFAULT_MIN_FREE_RATIO) -> None:
    """余量不够就抛 `DiskSpaceError`。

    为什么在**开建之前**拦：docker 构建把磁盘写满之后，倒霉的不只是这次构建 ——
    daemon 自己也会开始报错，正在跑的评测容器跟着一起崩，而那时候的报错信息
    （"no space left on device" 出现在某个 pytest 的输出里）根本指不到真正的原因。
    """
    if headroom.free_ratio >= min_free_ratio:
        return
    raise DiskSpaceError(
        f"磁盘剩余不足，拒绝开建。{headroom.describe()}，"
        f"低于阈值 {min_free_ratio:.0%}。"
        "先跑 `python -m cli.images gc --yes` 回收无引用的镜像，"
        "或者调高 IMAGE_DISK_MIN_FREE_RATIO"
    )


# ══════════════════════════════════════════════════════════════
# 构建
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """一次构建之后我们知道的全部事实。**只有事实，没有结论。**"""

    tag: str
    layer: str
    image_id: str
    digest: str | None
    recipe_hash: str
    #: 整个构建被跳过了（镜像上的配方哈希和这次算出来的一样）。
    skipped: bool
    #: 构建日志里有几步命中了 docker 的层缓存。
    cache_hits: int
    #: 一共几步。
    steps: int
    duration_s: float
    log: str
    log_truncated: bool = False
    #: 镜像里 `pip freeze` 的结果，读不到就是 None。
    lock: str | None = None
    #: 镜像里实际的 pytest 版本。
    pytest_version: str | None = None
    smoke: SmokeReport | None = None

    @property
    def image_ref(self) -> str:
        """引用这个镜像时该用的字符串。有 digest 就用 digest（协议 C-36）。"""
        return digest_reference(self.tag, self.digest) or self.tag


def _truncate_log(text: str, limit: int = MAX_BUILD_LOG_BYTES) -> tuple[str, bool]:
    """日志太长就掐头留尾。构建失败的原因通常在最后，装了什么在最前，中间是噪音。"""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    head = raw[: limit // 2].decode("utf-8", errors="ignore")
    tail = raw[-(limit // 2) :].decode("utf-8", errors="ignore")
    dropped = len(raw) - limit
    return f"{head}\n\n… 中间省略 {dropped} 字节 …\n\n{tail}", True


def _stream_build(events: Iterator[Any]) -> tuple[str, int, int, str | None]:
    """把 docker 构建的事件流收成 (日志, 缓存命中数, 步数, 出错信息)。

    经典构建器返回的是 `{"stream": "..."}` 一行行往外吐，出错时多一条
    `{"error": "...", "errorDetail": {...}}`。**出错事件之后流照样会结束**，
    不会抛异常，所以这里必须显式检查 —— 漏了它，构建失败会被当成成功。
    """
    chunks: list[str] = []
    cache_hits = 0
    steps = 0
    error: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if "error" in event:
            error = str(event.get("error"))
            chunks.append(f"\n[error] {error}\n")
            continue
        text = str(event.get("stream") or "")
        if not text:
            continue
        chunks.append(text)
        if "Using cache" in text:
            cache_hits += 1
        if text.startswith("Step "):
            steps += 1
    return "".join(chunks), cache_hits, steps, error


def build_image(
    *,
    context: Path,
    tag: str,
    labels: Mapping[str, str],
    dockerfile: str = "Dockerfile",
    buildargs: Mapping[str, str] | None = None,
    nocache: bool = False,
    client: Any = None,
) -> tuple[ImageInfo, str, int, int]:
    """调 daemon 建一个镜像，返回 (镜像身份, 日志, 缓存命中数, 步数)。

    走 docker SDK 的低层 `api.build()`，不 shell 出去调 `docker build`：
    事件流直接就是我们要的构建日志，不用去解析别人的终端输出，
    也不用担心管道把退出码吃掉（§8.8 坑 ①）。

    **不自动拉基础镜像**（`pull=False`），和 `run_in_container` 一个口径（ADR-008）。
    底座不在本地就直接报错，不要在建镜像时顺手把网络问题混进来。
    """
    docker_client = client or get_docker_client()
    try:
        events = docker_client.api.build(
            path=str(context),
            dockerfile=dockerfile,
            tag=tag,
            labels=dict(labels),
            buildargs=dict(buildargs or {}),
            rm=True,
            forcerm=True,
            nocache=nocache,
            pull=False,
            decode=True,
        )
        log, cache_hits, steps, error = _stream_build(events)
    except (APIError, DockerException, OSError) as exc:
        raise ImageBuildError(f"构建 {tag} 失败：{exc}") from exc

    if error:
        tail = "\n".join(log.strip().splitlines()[-25:])
        raise ImageBuildError(f"构建 {tag} 失败：{error}\n--- 日志末尾 ---\n{tail}")

    return inspect_image(tag, client=docker_client), log, cache_hits, steps


def existing_recipe_hash(tag: str, *, client: Any = None) -> str | None:
    """镜像现在带的配方哈希。镜像不在、或者没这个标签，都返回 None。"""
    docker_client = client or get_docker_client()
    try:
        image = docker_client.images.get(tag)
    except ImageNotFound:
        return None
    except (DockerException, OSError) as exc:
        raise SandboxError(f"查镜像 {tag} 失败：{exc}") from exc
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    value = labels.get(LABEL_RECIPE_HASH)
    return str(value) if value else None


def _read_file_from_image(tag: str, path: str, *, client: Any = None) -> str | None:
    """从镜像里读一个文本文件出来。读不到返回 None。

    起个容器 `cat` 一下。断网、非 root、限额照给 —— 顺带证明了这个镜像在
    评测时那套约束下起得来，起不来的话这里就会当场暴露。
    """
    result = run_in_container(
        ContainerSpec(
            image=tag,
            command=["cat", path],
            timeout_s=60,
            stage=Stage.TEST,
            limits=SMOKE_LIMITS,
            network=NetworkMode.NONE,
            env=build_env(),
            run_id="images-read",
        ),
        client=client,
    )
    return result.stdout if result.ok else None


# ══════════════════════════════════════════════════════════════
# 建完自查
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class SmokeReport:
    """自查结果。"""

    passed: bool
    #: `bench-import-check` 的输出。
    import_output: str = ""
    import_exit_code: int | None = None
    #: `pytest --collect-only` 的输出（截过）。
    collect_output: str = ""
    collect_exit_code: int | None = None
    collected: int | None = None
    problems: tuple[str, ...] = ()


#: 从 `pytest --collect-only -q` 的收尾行里抠出用例数，形如 `128 tests collected in 0.42s`。
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")


def _run_smoke_command(
    tag: str, workspace: Path, command: Sequence[str], *, client: Any = None
) -> Any:
    """在镜像里跑一条自查命令，工作区挂在 `/workspace`。

    三处刻意的选择：

    - **断网**（`NetworkMode.NONE`）。评测时的测试阶段就是断网的（协议 C-31、C-35），
      自查也断网，才能证明这个镜像离线可用。装漏了的依赖会在这里现形，
      而不是等到跑第一道题时才发现。
    - **跟着宿主机 uid 跑，不改成 root。** 容器规格里有 `cap_drop=ALL`，root 因此
      丢了 `CAP_DAC_OVERRIDE`，反而写不进挂进来的工作区（§8.8 坑 ②）。
    - **命令用列表，不过 shell。** 不需要 shell，也就不会踩管道吃退出码那一条。
    """
    return run_in_container(
        ContainerSpec(
            image=tag,
            command=list(command),
            timeout_s=SMOKE_TIMEOUT_S,
            stage=Stage.TEST,
            limits=SMOKE_LIMITS,
            network=NetworkMode.NONE,
            mounts=(BindMount.workspace(workspace),),
            workdir=WORKSPACE_TARGET,
            env=build_env(),
            run_id="images-smoke",
        ),
        client=client,
    )


def smoke_check(tag: str, *, recipe: EnvRecipe, workspace: Path, client: Any = None) -> SmokeReport:
    """建完之后当场验两件事，验不过就让这次构建算失败。

    **第一件：`import X` 拿到的是挂进来的工作区那一份吗？**

    env 镜像里躺着一份仓库快照（`/opt/repo`），装依赖要用它。评测时挂进来的
    `/workspace` 是另一个 commit，还被被测 AI 改过。要是 import 解析到了镜像里
    那一份，被测 AI 的改动根本不会被执行 —— 测试照跑，甚至可能全绿，
    而 Oracle 哨兵会从 100% 悄悄掉下去，日志里一点异常都没有。

    平铺布局（包目录直接在仓库根）碰巧没事，src 布局就会中招。渲染出来的
    `.pth` 是为了解决它，但**不保证有效**：pip 的 editable 安装有两种实现，
    新的那种注册的是 meta path finder，优先级高于 `sys.path`，`.pth` 插不进去。
    用哪一种取决于 setuptools 版本和仓库布局，猜不准。

    所以不猜：验一次，不对就当场红掉。这和 E2-T1「物化完自查树哈希、
    `export-ignore` 缺文件当场拦下」是同一个套路 —— 宁可建镜像时报错，
    也不要跑完 300 次评测才发现补丁根本没生效。

    **第二件：`pytest --collect-only` 收得到用例吗？**

    退出码 4 加"零条用例"是 `addopts` 引用了没装的插件的典型症状（§8.8 坑 ⑥）。
    这种环境建出来是能用的假象：真跑起来一条用例都不执行，而判定引擎会把
    "用例不存在"算成失败，解决率无声无息地偏低。
    """
    problems: list[str] = []
    report_kwargs: dict[str, Any] = {}

    if recipe.import_check:
        result = _run_smoke_command(
            tag,
            workspace,
            ["bench-import-check", "--root", WORKSPACE_TARGET, *recipe.import_check],
            client=client,
        )
        report_kwargs["import_output"] = (result.stdout + result.stderr).strip()
        report_kwargs["import_exit_code"] = result.exit_code
        if not result.ok:
            problems.append(
                f"import 自查没过（退出码 {result.exit_code}）："
                f"{recipe.environment_id} 的包没有解析到 {WORKSPACE_TARGET} 底下"
            )

    if recipe.collect_check:
        result = _run_smoke_command(
            tag,
            workspace,
            [
                "python",
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                *recipe.test_args,
            ],
            client=client,
        )
        output = (result.stdout + result.stderr).strip()
        report_kwargs["collect_output"] = output[-4000:]
        report_kwargs["collect_exit_code"] = result.exit_code
        match = _COLLECTED_RE.search(output)
        collected = int(match.group(1)) if match else None
        report_kwargs["collected"] = collected
        if not result.ok:
            problems.append(
                f"pytest --collect-only 退出码 {result.exit_code}。"
                "两个常见原因：addopts 里引用的插件没装（§8.8 坑 ⑥），"
                "那种环境跑起来一条用例都不会执行；"
                "或者收集范围不对 —— 配方的 test_args 应当照抄仓库自己的测试命令，"
                "从仓库根收全部会扫到示例脚本和同名文件"
            )
        elif not collected:
            problems.append("pytest --collect-only 一条用例都没收到")

    return SmokeReport(passed=not problems, problems=tuple(problems), **report_kwargs)


# ══════════════════════════════════════════════════════════════
# 三层的入口
# ══════════════════════════════════════════════════════════════


def build_base_image(
    *,
    context: Path,
    tag: str = BASE_TAG,
    nocache: bool = False,
    force: bool = False,
    client: Any = None,
) -> BuildOutcome:
    """建第一层。配方哈希就是 Dockerfile 本身的哈希 —— 它是手写的，没有别的输入。"""
    dockerfile_text = (Path(context) / "Dockerfile").read_text(encoding="utf-8")
    recipe_hash = hashlib.sha256(
        f"{RECIPE_HASH_VERSION}\n{BASE_IMAGE_REF}\n{dockerfile_text}".encode()
    ).hexdigest()
    labels = {
        BENCH_LABEL: BENCH_LABEL_VALUE,
        LABEL_LAYER: LAYER_BASE,
        LABEL_RECIPE_HASH: recipe_hash,
    }

    started = time.monotonic()
    if not force and existing_recipe_hash(tag, client=client) == recipe_hash:
        info = inspect_image(tag, client=client)
        return BuildOutcome(
            tag=tag,
            layer=LAYER_BASE,
            image_id=info.image_id,
            digest=info.digest,
            recipe_hash=recipe_hash,
            skipped=True,
            cache_hits=0,
            steps=0,
            duration_s=time.monotonic() - started,
            log="",
        )

    info, log, cache_hits, steps = build_image(
        context=Path(context), tag=tag, labels=labels, nocache=nocache, client=client
    )
    log, truncated = _truncate_log(log)
    return BuildOutcome(
        tag=tag,
        layer=LAYER_BASE,
        image_id=info.image_id,
        digest=info.digest,
        recipe_hash=recipe_hash,
        skipped=False,
        cache_hits=cache_hits,
        steps=steps,
        duration_s=time.monotonic() - started,
        log=log,
        log_truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """建 env 镜像用的仓库快照。"""

    path: Path
    commit: str
    tree_sha: str
    file_count: int


def ensure_snapshot_repo(
    recipe: EnvRecipe,
    *,
    mirror_root: Path,
    snapshot_root: Path,
    timeout_s: int = 1800,
    allow_fetch: bool = True,
) -> Path:
    """找到（必要时拉下来）一个含 `snapshot_commit` 的裸仓库，返回它的路径。

    找的顺序：

    1. `var/mirrors/` 里的完整镜像 —— Golden 题走这条，题目验证流水线也用它。
    2. `var/build-snapshots/` 里的浅克隆 —— 建镜像专用。

    **为什么给建镜像单开一个目录，而不是往 `var/mirrors/` 里塞浅克隆。**

    这台机器过代理拉 GitHub 很不稳：`git clone --mirror milvus-io/pymilvus` 跑了
    4 分半之后 `Connection reset by peer` 挂掉（2026-09-08 实测，E8-T1 也栽过同样的
    坑）；换成 `--depth 1 --single-branch` 一次就成，2.3 MB、几秒钟。

    建 env 镜像只需要**一个** commit 的文件树，浅克隆完全够用。但题目验证要按各题
    的 `base_commit` 物化工作区，需要完整历史 —— 把浅克隆放进 `var/mirrors/`，
    `MirrorManager.exists()` 就会返回真，后面的代码会以为历史是全的，然后在某个
    具体题目上莫名其妙地找不到 commit。两个目录分开，这个歧义就不存在。
    """
    from app.sandbox.git_cli import GitError, run_git  # 局部导入，避免模块级循环依赖

    mirror = Path(mirror_root) / f"{recipe.repo_name.replace('/', '__')}.git"
    if (mirror / "HEAD").is_file():
        check = run_git(
            ["cat-file", "-e", f"{recipe.snapshot_commit}^{{commit}}"],
            cwd=mirror,
            timeout_s=60,
            check=False,
        )
        if check.returncode == 0:
            return mirror

    shallow = Path(snapshot_root) / f"{recipe.repo_name.replace('/', '__')}.git"
    if (shallow / "HEAD").is_file():
        check = run_git(
            ["cat-file", "-e", f"{recipe.snapshot_commit}^{{commit}}"],
            cwd=shallow,
            timeout_s=60,
            check=False,
        )
        if check.returncode == 0:
            return shallow

    if not allow_fetch:
        raise ImageBuildError(
            f"{recipe.repo_name} 的快照 {recipe.snapshot_commit[:12]} 本地没有，"
            f"而这次不允许联网拉取。先跑一次不带 --offline 的构建"
        )
    if recipe.repo_url.startswith("golden://"):
        raise ImageBuildError(
            f"{recipe.repo_name} 是本地 Golden 仓库，但 {mirror} 里没有 "
            f"{recipe.snapshot_commit[:12]}。先跑 `make golden` 重建本地镜像"
        )

    shallow.parent.mkdir(parents=True, exist_ok=True)
    if shallow.exists():
        shutil.rmtree(shallow)
    try:
        # `--depth 1` 只能拉引用的顶端，所以先建空仓库再 fetch 指定的 sha ——
        # 这样配方里写死哪个 commit 就拉哪个，不会因为上游又推了几个提交而变。
        run_git(["init", "--bare", "--quiet", str(shallow)], timeout_s=60)
        run_git(["remote", "add", "origin", "--", recipe.repo_url], cwd=shallow, timeout_s=60)
        run_git(
            ["fetch", "--depth", "1", "--quiet", "origin", recipe.snapshot_commit],
            cwd=shallow,
            timeout_s=timeout_s,
        )
    except GitError as exc:
        raise ImageBuildError(
            f"拉 {recipe.repo_name} 的快照 {recipe.snapshot_commit[:12]} 失败：{exc}。"
            "这台机器过代理拉 GitHub 容易断，重试一次通常就好"
        ) from exc
    return shallow


def materialize_snapshot(
    recipe: EnvRecipe, *, mirror_path: Path, dest: Path, timeout_s: int = 1800
) -> Snapshot:
    """把快照 commit 的文件树导出到 `dest`，当 docker 构建上下文用。

    直接复用 `materialize_workspace()`：它已经把"树哈希必须和上游一致""历史只留
    一个提交"两条不变量固化成自查了（E2-T1）。构建上下文用不上那个 `.git`，
    由 `.dockerignore` 排掉。
    """
    workspace = materialize_workspace(
        mirror_path=mirror_path,
        base_commit=recipe.snapshot_commit,
        dest=dest,
        timeout_s=timeout_s,
    )
    return Snapshot(
        path=workspace.path,
        commit=workspace.base_commit,
        tree_sha=workspace.tree_sha,
        file_count=workspace.file_count,
    )


def prepare_env_context(
    recipe: EnvRecipe, *, snapshot: Snapshot, context: Path, base_tag: str = BASE_TAG
) -> str:
    """在 `context` 目录里摆好构建上下文，返回渲染出来的 Dockerfile 文本。

    上下文长这样：

        context/
          Dockerfile        渲染出来的
          .dockerignore     排掉 .git
          repo/             快照的文件树（软链或复制）
    """
    context = Path(context)
    context.mkdir(parents=True, exist_ok=True)
    dockerfile_text = render_env_dockerfile(recipe, base_tag=base_tag)
    (context / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
    # `.git` 必须排掉：它是物化时建的一个只有一个提交的仓库，进镜像既没用又占空间，
    # 而且会让"镜像里有没有 git 历史"这个防泄题问题多一个要操心的地方
    (context / ".dockerignore").write_text(".git\n", encoding="utf-8")

    repo_dir = context / "repo"
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    shutil.copytree(snapshot.path, repo_dir, ignore=shutil.ignore_patterns(".git"))
    return dockerfile_text


def build_env_image(
    recipe: EnvRecipe,
    *,
    context: Path,
    snapshot: Snapshot,
    base_tag: str = BASE_TAG,
    base_ref: str | None = None,
    nocache: bool = False,
    force: bool = False,
    skip_smoke: bool = False,
    client: Any = None,
) -> BuildOutcome:
    """建第二层：某个环境的镜像。

    `base_ref` 是底座的 digest（`bench-base` 建完之后拿到的那个），进配方哈希 ——
    底座换了，上面装出来的依赖可能整个不一样，必须跟着重建。
    """
    tag = recipe.image_tag
    dockerfile_text = prepare_env_context(
        recipe, snapshot=snapshot, context=context, base_tag=base_tag
    )
    recipe_hash = compute_recipe_hash(
        dockerfile=dockerfile_text,
        base_ref=base_ref or base_tag,
        snapshot_tree_sha=snapshot.tree_sha,
        recipe=recipe,
    )
    labels = {
        BENCH_LABEL: BENCH_LABEL_VALUE,
        LABEL_LAYER: LAYER_ENV,
        LABEL_ENVIRONMENT_ID: recipe.environment_id,
        LABEL_RECIPE_HASH: recipe_hash,
    }

    started = time.monotonic()
    if not force and existing_recipe_hash(tag, client=client) == recipe_hash:
        info = inspect_image(tag, client=client)
        return BuildOutcome(
            tag=tag,
            layer=LAYER_ENV,
            image_id=info.image_id,
            digest=info.digest,
            recipe_hash=recipe_hash,
            skipped=True,
            cache_hits=0,
            steps=0,
            duration_s=time.monotonic() - started,
            log="",
        )

    info, log, cache_hits, steps = build_image(
        context=Path(context), tag=tag, labels=labels, nocache=nocache, client=client
    )
    log, truncated = _truncate_log(log)

    lock = _read_file_from_image(tag, LOCK_PATH, client=client)
    pytest_version = _read_file_from_image(tag, f"{BENCH_DIR}/pytest-version.txt", client=client)

    smoke = None
    if not skip_smoke:
        smoke = smoke_check(tag, recipe=recipe, workspace=snapshot.path, client=client)

    outcome = BuildOutcome(
        tag=tag,
        layer=LAYER_ENV,
        image_id=info.image_id,
        digest=info.digest,
        recipe_hash=recipe_hash,
        skipped=False,
        cache_hits=cache_hits,
        steps=steps,
        duration_s=time.monotonic() - started,
        log=log,
        log_truncated=truncated,
        lock=lock,
        pytest_version=(pytest_version or "").strip() or None,
        smoke=smoke,
    )
    if smoke is not None and not smoke.passed:
        raise SmokeCheckError(
            f"{tag} 建出来了，但自查没过：\n  "
            + "\n  ".join(smoke.problems)
            + f"\n--- bench-import-check ---\n{smoke.import_output}"
            + f"\n--- pytest --collect-only（末尾）---\n{smoke.collect_output[-1500:]}",
            outcome=outcome,
        )
    return outcome


def agent_image_tag(environment_id: str, agent: str) -> str:
    """第三层的 tag：`bench-agent:{env}-{agent}`。"""
    tag = f"{AGENT_TAG_PREFIX}:{environment_id}-{agent}"
    part = tag.split(":", 1)[1]
    if not _TAG_PART_RE.match(part):
        raise RecipeError(f"拼出来的镜像 tag 不合法：{tag}")
    return tag


def build_agent_image(
    *,
    context: Path,
    environment_id: str,
    agent: str,
    base_tag: str,
    base_ref: str | None = None,
    nocache: bool = False,
    force: bool = False,
    client: Any = None,
) -> BuildOutcome:
    """建第三层：在某个环境镜像上加一层被测 AI 的 CLI。

    复用 `images/{agent}/Dockerfile`，只把 `BASE_IMAGE` 这个构建参数换掉 ——
    那两份 Dockerfile 的其余部分（钉死的版本、关掉遥测、git 身份）一个字不用动。
    """
    dockerfile_text = (Path(context) / "Dockerfile").read_text(encoding="utf-8")
    recipe_hash = hashlib.sha256(
        json.dumps(
            {
                "version": RECIPE_HASH_VERSION,
                "base_ref": base_ref or base_tag,
                "dockerfile": dockerfile_text,
                "agent": agent,
                "environment_id": environment_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    tag = agent_image_tag(environment_id, agent)
    labels = {
        BENCH_LABEL: BENCH_LABEL_VALUE,
        LABEL_LAYER: LAYER_AGENT,
        LABEL_ENVIRONMENT_ID: environment_id,
        LABEL_AGENT: agent,
        LABEL_RECIPE_HASH: recipe_hash,
    }

    started = time.monotonic()
    if not force and existing_recipe_hash(tag, client=client) == recipe_hash:
        info = inspect_image(tag, client=client)
        return BuildOutcome(
            tag=tag,
            layer=LAYER_AGENT,
            image_id=info.image_id,
            digest=info.digest,
            recipe_hash=recipe_hash,
            skipped=True,
            cache_hits=0,
            steps=0,
            duration_s=time.monotonic() - started,
            log="",
        )

    info, log, cache_hits, steps = build_image(
        context=Path(context),
        tag=tag,
        labels=labels,
        buildargs={"BASE_IMAGE": base_tag},
        nocache=nocache,
        client=client,
    )
    log, truncated = _truncate_log(log)
    return BuildOutcome(
        tag=tag,
        layer=LAYER_AGENT,
        image_id=info.image_id,
        digest=info.digest,
        recipe_hash=recipe_hash,
        skipped=False,
        cache_hits=cache_hits,
        steps=steps,
        duration_s=time.monotonic() - started,
        log=log,
        log_truncated=truncated,
    )


# ══════════════════════════════════════════════════════════════
# 回收
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class GcCandidate:
    """一个可以回收的镜像。"""

    image_id: str
    tags: tuple[str, ...]
    layer: str | None
    environment_id: str | None
    #: **删掉它真能腾出来的字节数**，也就是不和别的镜像共用的那部分。
    size_bytes: int
    reason: str

    @property
    def display(self) -> str:
        name = self.tags[0] if self.tags else f"<无标签> {self.image_id[7:19]}"
        return f"{name}（独占 {self.size_bytes / 2**30:.2f} GiB）—— {self.reason}"


@dataclass(frozen=True, slots=True)
class ImageDiskUsage:
    """一个镜像在磁盘上占多少。"""

    #: 解包之后的总字节数，含和别的镜像共用的层。
    total_bytes: int
    #: 其中和别的镜像共用的部分。
    shared_bytes: int

    @property
    def unique_bytes(self) -> int:
        """只有这个镜像用到的部分 —— 删掉它能腾出来的就是这些。"""
        return max(self.total_bytes - self.shared_bytes, 0)


def image_disk_usage(*, client: Any = None) -> dict[str, ImageDiskUsage]:
    """问 daemon 每个镜像在磁盘上到底占多少，返回 `{image_id: 用量}`。

    **不能用 `images.list()` 里那个 `Size` 字段。** 在 containerd 镜像存储下，
    那个字段报的是内容仓库里**压缩后**的大小，而磁盘上躺着的是解包后的快照 ——
    实测差三到四倍（`bench-base:py311` 一个报 0.20 GiB、一个报 0.78 GiB；
    装了 torch 的那个 3.42 GiB 对 10.51 GiB）。

    用错的后果不是"数字不好看"：`gc` 会说自己只能回收 15 GiB 而实际是 50 GiB，
    磁盘水位那套账也跟着错。所以走 `/system/df`，它报的和 `docker images` 一致。

    这个接口要遍历全部镜像和层，比 `images.list()` 慢，只在 `list` / `gc` 这种
    人在等结果的命令里用，别放进构建的热路径。
    """
    docker_client = client or get_docker_client()
    try:
        payload = docker_client.api.df()
    except (AttributeError, DockerException, OSError) as exc:
        logger.warning("拿不到 docker system df，镜像大小按压缩后的算", error=str(exc))
        return {}
    usage: dict[str, ImageDiskUsage] = {}
    for entry in payload.get("Images") or []:
        image_id = str(entry.get("Id") or "")
        if not image_id:
            continue
        usage[image_id] = ImageDiskUsage(
            total_bytes=int(entry.get("Size") or 0),
            # 没有共用层时 docker 报 -1，不是 0
            shared_bytes=max(int(entry.get("SharedSize") or 0), 0),
        )
    return usage


def _digest_of(attrs: Mapping[str, Any]) -> str | None:
    """镜像的 RepoDigest（`sha256:...` 那一段）。没有就返回 None。"""
    for entry in sorted(str(d) for d in (attrs.get("RepoDigests") or [])):
        if "@" in entry:
            return entry.split("@", 1)[1]
    return None


def _label_of(attrs: Mapping[str, Any], name: str) -> str | None:
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    value = labels.get(name)
    return str(value) if value else None


def gc_candidates(
    *,
    live_environment_ids: Sequence[str],
    keep_digests: Sequence[str] = (),
    client: Any = None,
) -> list[GcCandidate]:
    """列出可以删的镜像。

    **只看我们自己打了 `bench.owner` 标签的镜像。** 手工建的
    `bench-golden:py311`、`bench-agent:py311-aider` 没有这个标签，所以碰不到 ——
    回收命令宁可少删，也不能误删别的项目的东西。第一层（base）永远保留。

    两类可回收：

    1. **环境已经不存在**：`bench.environment_id` 不在 `live_environment_ids` 里。
       活名单由调用方给出，等于"配方目录里有的" ∪ "`environment_specs` 表里有的"。
    2. **被新版本顶掉的旧镜像**：重建一个 env 之后，上一版会丢掉 tag 留在本地。
       环境本身还活着，所以第 1 条收不到它们 —— 实测跑几轮就攒下 10 个、1 GB
       （2026-09-08，`images list` 里一眼看见的）。没有 tag、digest 也不在
       `keep_digests` 里，就是没人引用了。

    `keep_digests` 是护栏，调用方要把 `environment_specs.image_digest` **整列**
    传进来（不只是活环境那几行）。协议 C-36 要求运行记录按 digest 引用镜像，
    删掉一个还被记录着的 digest，等于把那次实验的可复现性抹掉了。
    """
    docker_client = client or get_docker_client()
    live = set(live_environment_ids)
    keep = {d for d in keep_digests if d}
    disk = image_disk_usage(client=docker_client)
    try:
        images = docker_client.images.list(filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"})
    except (DockerException, OSError) as exc:
        raise SandboxError(f"列镜像失败：{exc}") from exc

    candidates: list[GcCandidate] = []
    for image in images:
        attrs = image.attrs or {}
        layer = _label_of(attrs, LABEL_LAYER)
        env_id = _label_of(attrs, LABEL_ENVIRONMENT_ID)
        tags = tuple(str(t) for t in (image.tags or []))
        image_id = str(image.id)
        # 判据只看"有没有 tag"，**不看 `bench.layer`**。
        #
        # label 会被子镜像继承：建 env 时产生的中间层顶着从 bench-base 继承来的
        # `bench.layer=base`，靠 layer 判断就会把它们当成第一层一律保留，
        # 于是 gc 永远收敛不掉（2026-09-08 实测，剩了 3 个 0.65 GiB 的中间层）。
        # tag 是我们自己打上去的，不会被继承。
        if tags:
            # 有 tag = 现役镜像。只有"环境已经不存在"才删；bench-base:py311 没有
            # 环境 id，因此永远落不到这一条上
            if not env_id or env_id in live:
                continue
            reason = f"环境 {env_id} 已经没有配方也没有库里的记录"
        else:
            # 没 tag = 被新版本顶掉的旧构建，除非 digest 还被库里记着
            if image_id in keep or _digest_of(attrs) in keep:
                # 某次实验按这个 digest 引用过，删了就毁了那次的可复现性（C-36）
                continue
            reason = (
                f"环境 {env_id} 的旧版本，已经没有 tag 也没有被库里引用"
                if env_id
                else "没有环境标签的悬空镜像"
            )
        candidates.append(
            GcCandidate(
                image_id=image_id,
                tags=tags,
                layer=layer,
                environment_id=env_id,
                # 报"删了能腾出多少"，不是"这个镜像总共多大" —— 后者把和现役镜像
                # 共用的层也算进去了，删了根本腾不出来
                size_bytes=(
                    disk[image_id].unique_bytes if image_id in disk else int(attrs.get("Size") or 0)
                ),
                reason=reason,
            )
        )
    return sorted(candidates, key=lambda c: (c.layer or "", c.tags, c.image_id))


def remove_image(candidate: GcCandidate, *, client: Any = None) -> str | None:
    """删一个镜像。删不掉（还有容器在用）就返回原因，不抛异常。"""
    docker_client = client or get_docker_client()
    target = candidate.tags[0] if candidate.tags else candidate.image_id
    try:
        docker_client.images.remove(target, force=False)
    except (APIError, DockerException, OSError) as exc:
        return f"{target}：{exc}"
    return None


__all__ = [
    "AGENT_TAG_PREFIX",
    "BASE_IMAGE_REF",
    "BASE_TAG",
    "DEFAULT_MIN_FREE_RATIO",
    "ENV_TAG_PREFIX",
    "LABEL_AGENT",
    "LABEL_ENVIRONMENT_ID",
    "LABEL_LAYER",
    "LABEL_RECIPE_HASH",
    "LAYER_AGENT",
    "LAYER_BASE",
    "LAYER_ENV",
    "BuildOutcome",
    "DiskHeadroom",
    "DiskSpaceError",
    "EnvRecipe",
    "GcCandidate",
    "ImageBuildError",
    "ImageDiskUsage",
    "RecipeError",
    "SmokeCheckError",
    "SmokeReport",
    "Snapshot",
    "agent_image_tag",
    "build_agent_image",
    "build_base_image",
    "build_env_image",
    "build_image",
    "compute_recipe_hash",
    "disk_headroom",
    "docker_root_dir",
    "existing_recipe_hash",
    "gc_candidates",
    "image_disk_usage",
    "load_recipe",
    "load_recipes",
    "materialize_snapshot",
    "parse_recipe",
    "prepare_env_context",
    "remove_image",
    "render_env_dockerfile",
    "require_disk",
    "smoke_check",
    "workspace_pth_line",
]
