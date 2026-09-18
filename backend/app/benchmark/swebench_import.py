"""SWE-bench Verified 官方题导入（E1-T7，`03-benchmark-spec.md` §8.6）。

一句话：**把官方数据集里的一道题原样翻成我们的 `TaskDefinition`，环境直接用官方镜像。**

    HF 数据集 500 行 ─▶ 离线筛（判定引擎认不认、题面合不合规）─▶ 固定种子分层抽样
        ─▶ 拉官方镜像 + 备好 git 镜像 ─▶ 组装题目入库 ─▶ `cli.validate run` 八步验证

这批题**只用于校准**（MET-01：证明判定引擎对官方任务判得和官方一致），
`dataset_id` 单独是 `swebench-verified-subset`，不混进 `benchmark-cn-v1` 的统计（§8.6）。

## 字段映射（§8.6 那张表，逐项落实）

| 官方字段 | 我们的字段 |
|:---|:---|
| `instance_id` | `task_id`（格式 `{owner}__{repo}-{pr}` 本来就和 SWE-bench 兼容）|
| `repo` | `repo_name`，`repo_url` 拼 `https://github.com/{repo}.git` |
| `base_commit` | `base_commit` |
| `problem_statement` | `issue_body`（第一行同时充当 `issue_title`，官方没有单独的标题字段）|
| `patch` | `gold_patch` |
| `test_patch` | `test_patch` |
| `FAIL_TO_PASS` | `fail_to_pass`（官方存的是 JSON 字符串，这里解开；被空白切断的参数化 id 剔掉）|
| `PASS_TO_PASS` | `pass_to_pass`（同上；再剔掉 `[100%]` 这种不是用例 ID 的残渣，见下）|
| `environment_setup_commit` | 环境分桶依据：退回自建时按它建桶，走官方镜像时记进 tag |

## 环境：优先官方镜像，一道题一个

SWE-bench 给**每道题**发布了一个镜像 `swebench/sweb.eval.x86_64.<instance_id>`
（Docker Hub 不允许仓库名里有 `__`，官方把它换成了 `_1776_`），里面是
`/testbed` 下的仓库（已 `reset --hard` 到 `base_commit`）加一个装好依赖的
conda 环境 `testbed`。镜像是按题发的，所以 `environment_specs` 也一道题一行
（`swebench__<instance_id>`）。`environment_setup_commit` 分的桶对应的是官方的
**env 层**镜像（同一个桶共享同一层），退回自建时就按桶建一个 `bench-env`。

## 用官方镜像跑测试要绕过的三件事

1. **conda 环境没激活。** 我们起容器时不走 shell，`.bashrc` 里那句 `conda activate`
   不会执行。所以测试命令直接写 `/opt/miniconda3/envs/testbed/bin/python -m pytest`，
   不依赖 `PATH`。
2. **`pip install -e .` 指向 `/testbed`，不是我们的 `/workspace`。** 不处理的话，
   测试 import 到的是镜像里没打补丁的那份代码，Oracle 必然 0%。处理办法是
   `env PYTHONPATH=/workspace[/src|/lib]` —— `PYTHONPATH` 排在 site-packages
   前面，也就排在 editable 安装的 `.pth` / import hook 前面。`src` / `lib` 布局
   的仓库（flask、pytest、matplotlib）要指到包所在的那一层，见 `IMPORT_ROOTS`。
3. **编译产物只在 `/testbed` 里。** astropy / matplotlib / scikit-learn 的 `.so`
   是 `pip install -e .` 时就地编译进 `/testbed` 的，`git archive` 物化出来的
   工作区没有。`pre_test_command` 把 `/testbed` 里**被 git 忽略的文件**（`.so`、
   `*.egg-info`、生成的 `version.py`……）复制到工作区里**不存在**的位置：
   只拷 git 忽略的文件，就不可能盖掉任何源文件，也不可能把被测 AI 删掉的
   文件还原回来。

三件事有没有真的绕过去，不靠肉眼看：**Oracle 100% / Noop 0% 这对哨兵就是证明** ——
gold 补丁只存在于 `/workspace`，测试要是 import 了 `/testbed`，Oracle 就过不了。

## 抽样是固定种子分层随机（§8.6）

按 `repo` 分层，各层配额按比例分（最大余数法，非空层至少 1 道），层内用
`random.Random(f"{seed}/{repo}")` 打乱后取前 N 个。同一种子两次抽出同一批；
层内顺序是打乱后固定的，所以把总数从 50 调到 60 时，前 50 道不变、只是多出 10 道 ——
种子和代码进仓库，名单是算出来的不是手工挑的。

## 判定引擎只认 pytest 的 junit 报告；沙箱断网

`django/django`（231 道）和 `sympy/sympy`（75 道）的官方测试命令分别是自带的
`runtests.py`（unittest）和 `bin/test`，都不出逐用例的 junit 报告，官方靠
解析终端日志判定；我们的判定引擎按 §7.2(4) 只吃机器可解析的逐用例报告。
`psf/requests`（8 道）的测试要打 httpbin.org，沙箱按 C-31 断网，实测在 base 上
P2P 就挂一小半（见 `EXCLUDED_REPOS` 的注释）。
这三家在导入漏斗的第一层就被剔掉，**如实计数，不硬凑**。
"""

from __future__ import annotations

import json
import random
import re
import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.benchmark.assembly import DEFAULT_REPORT_PATH, derive_difficulty
from app.benchmark.schema import TaskDefinition
from app.domain.enums import IssueLanguage
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import DEFAULT_PROTECTED_PATTERNS, is_protected, protected_hits
from app.sandbox.container import WORKSPACE_TARGET

#: 这批题的 `dataset_id`，也是 `benchmark_sets.slug`（§8.1 的 L2'）。
DATASET_ID = "swebench-verified-subset"

#: HuggingFace 上的官方数据集。
HF_DATASET = "princeton-nlp/SWE-bench_Verified"

#: 抽样种子。**写死一个常量**，不然"固定种子"就是一句空话。
DEFAULT_SEED = 20260915
#: §8.6 写的是 50–100 题，任务卡 AC 3 定的是 50；2026-09-16 晚为补 MET-05 的缺口抽到 75，
#: 2026-09-18 为给 MET-05 留余量抽到 100（§8.6 的上限）。原 n75 的 75 道全部保留、相对顺序不变，
#: 新题按仓库插入，并非整个名单的前 75 个位置相同；v1 / v2 的已发布快照不动。
DEFAULT_SAMPLE_SIZE = 100

#: 官方镜像的命名。仓库名里的 `__` 在 Docker Hub 上是非法的，官方用 `_1776_` 顶替
#: （见 swebench 包 `test_spec.py` 的 `instance_image_key`），而且整个 id 小写。
OFFICIAL_IMAGE_NAMESPACE = "swebench"
OFFICIAL_IMAGE_ARCH = "x86_64"
OFFICIAL_IMAGE_TAG = "latest"

#: 官方镜像里 conda 环境的 python 和仓库所在目录。
OFFICIAL_ENV_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"
OFFICIAL_TESTBED = "/testbed"

#: 判定引擎能吃的仓库 → 包所在的源码根（相对仓库根）。
#:
#: 值是 `PYTHONPATH` 要指到的那一层：flask 和 pytest 是 `src/` 布局，
#: matplotlib 的包在 `lib/` 下，其余包就在仓库根。指错了不会报错，
#: 只会让测试 import 到镜像里 `/testbed` 那份没打补丁的代码 —— Oracle 会当场抓住。
IMPORT_ROOTS: Mapping[str, str] = {
    "astropy/astropy": "",
    "matplotlib/matplotlib": "lib",
    "mwaskom/seaborn": "",
    "pallets/flask": "src",
    "pydata/xarray": "",
    "pylint-dev/pylint": "",
    "pytest-dev/pytest": "src",
    "scikit-learn/scikit-learn": "",
    "sphinx-doc/sphinx": "",
}

#: 整仓库进不了池子的，以及为什么。前两家是判定引擎读不了报告，第三家是沙箱断网。
#:
#: `psf/requests` 是 2026-09-15 实测排除的：它的测试套件打 httpbin.org，而沙箱按协议
#: C-31 断网。`psf__requests-2317` 官方 133 条 P2P 在 base 上就挂 52 条、8 条 F2P 里
#: `test_HTTP_302_ALLOW_REDIRECT_GET` 这类全要联网，打上 gold 也过不了。
#: 这和 E8-T3 判 xorbitsai 不可用是同一个病：给沙箱开网不是解法，开了被测 AI 就能去
#: GitHub 抄补丁。官方 harness 跑测试时是有网的，所以官方能过、我们不能，不是题坏了。
EXCLUDED_REPOS: Mapping[str, str] = {
    "django/django": "官方测试命令是 tests/runtests.py（unittest），不出逐用例 junit 报告",
    "sympy/sympy": "官方测试命令是 bin/test（自带 runner），不出逐用例 junit 报告",
    "psf/requests": "测试套件要打 httpbin.org，沙箱断网（C-31）下 P2P 在 base 上就挂 52/133",
}

#: 跑测试的命令。和 `assembly.TEST_COMMAND_BASE` 差两处：没有 `--timeout`
#: （官方镜像里没有 pytest-timeout，加了整条命令直接报 unrecognized arguments），
#: 前面多了 `env PYTHONPATH=…`（理由见模块文档第 2 条）。
#: `{pythonpath}` 由 `pytest_command_for()` 按仓库填。
_TEST_COMMAND = (
    "env PYTHONPATH={pythonpath} " + OFFICIAL_ENV_PYTHON + " -m pytest -rA -p no:randomly "
    "-p no:cacheprovider --continue-on-collection-errors --junitxml=" + DEFAULT_REPORT_PATH
)

#: 声明的用例只跑一小段，但 astropy / matplotlib 光 import 就要几十秒，
#: scikit-learn 有的题 P2P 两千多条。给 30 分钟，比挖掘题的 480 秒宽。
OFFICIAL_TEST_TIMEOUT_S = 1800

#: `pre_test_command`：把 `/testbed` 里被 git 忽略的文件拷到工作区（模块文档第 3 条）。
#:
#: 写成 `python -c` 一段是因为容器里只有工作区这一个挂载点，塞不进脚本文件。
#: 逐条解释：
#:   - `git ls-files --others --ignored --exclude-standard`：只列**被忽略的未跟踪文件**，
#:     也就是构建产物。跟踪文件一个都不碰。
#:   - `-c safe.directory=*`：`/testbed` 是 root 建的，容器里我们是普通 uid，
#:     git 会拒绝"别人的仓库"；命令行 `-c` 属于受保护配置，能放行。
#:   - `__pycache__` / `.pyc` / `build/` 跳过：字节码是按源文件 mtime 校验的，
#:     拷过来也会重编；`build/` 是中间产物，就地编译的 `.so` 已经在包目录里。
#:   - 目标已存在就跳过：绝不覆盖工作区里的任何东西。
#: 官方最老的环境是 python 3.6（scikit-learn 0.2x），所以不能用 3.7 才有的 `capture_output=`
#: （2026-09-16 踩过：8 道 scikit-learn 题 S4 报"pre_test_command 失败"）。
_COPY_ARTIFACTS_SCRIPT = f"""\
import os, shutil, subprocess
src, dst = {OFFICIAL_TESTBED!r}, {WORKSPACE_TARGET!r}
listing = subprocess.run(
    ["git", "-c", "safe.directory=*", "-C", src, "ls-files", "-z",
     "--others", "--ignored", "--exclude-standard"],
    stdout=subprocess.PIPE, check=True,
).stdout.decode()
copied = 0
for rel in listing.split("\\0"):
    parts = rel.split("/")
    if not rel or rel.endswith(".pyc") or parts[0] == "build" or "__pycache__" in parts:
        continue
    target = os.path.join(dst, rel)
    if os.path.lexists(target):
        continue
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(os.path.join(src, rel), target, follow_symlinks=False)
    copied += 1
print("copied", copied, "ignored files from", src)
"""
#: 执行器用 `shlex.split` 拆命令，不过 shell，所以用 `shlex.quote` 把整段脚本包成一个参数
#: （单引号里换行原样保留；`json.dumps` 那种双引号写法里的 `\n` 会被原样交给 python）。
PRE_TEST_COMMAND = f"{OFFICIAL_ENV_PYTHON} -c {shlex.quote(_COPY_ARTIFACTS_SCRIPT)}"

#: 官方字段里的用例 ID 必须是 pytest 的 nodeid（含 `::`）。官方日志解析器偶尔会把
#: 进度百分比 `[100%]` 当成用例名塞进 PASS_TO_PASS（实测 2 条），这种不是用例，剔掉。
_NODEID_SEPARATOR = "::"

#: issue 正文里的泄题形式，和 `schema._check_issue_not_leaking` 是同一套判据。
#: 这里提前判一次是为了给漏斗一个明确的格子，而不是笼统的"schema 拒收"。
_LEAK_PR_URL = re.compile(r"https?://[^\s]*/(?:pull|merge_requests)/\d+", re.IGNORECASE)
_LEAK_DIFF_BLOCK = re.compile(r"^diff --git ", re.MULTILINE)

#: `issue_title` 列是 String(500)，取第一行再截一刀。
MAX_TITLE_CHARS = 200


class SwebenchImportError(RuntimeError):
    """导入过程中数据本身的问题（行缺字段、名单对不上……），附人话原因。"""


# ══════════════════════════════════════════════════════════════
# 官方数据行
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class VerifiedInstance:
    """官方数据集的一行，字段名照抄 HF 上的列名（大小写也照抄，好对照）。"""

    instance_id: str
    repo: str
    base_commit: str
    patch: str
    test_patch: str
    problem_statement: str
    hints_text: str
    created_at: str
    version: str
    FAIL_TO_PASS: tuple[str, ...]  # 照抄官方列名，大写也照抄
    PASS_TO_PASS: tuple[str, ...]
    environment_setup_commit: str
    #: Verified 附带的人工难度标注（`<15 min fix` 之类）。只进 tag，不替代 §7.8 的派生难度。
    difficulty: str = ""

    @property
    def pr_number(self) -> int:
        """`instance_id` 最后一段 `-数字` 就是 PR 号。"""
        return int(self.instance_id.rsplit("-", 1)[1])

    @property
    def owner(self) -> str:
        return self.repo.partition("/")[0]

    @property
    def name(self) -> str:
        return self.repo.partition("/")[2]


def _test_ids(value: Any) -> tuple[str, ...]:
    """官方的 F2P / P2P 列是 JSON **字符串**（`'["a", "b"]'`），不是列表。两种都吃。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise SwebenchImportError(f"FAIL_TO_PASS / PASS_TO_PASS 应当是列表，收到 {type(value)}")
    return tuple(str(item) for item in value)


def parse_instance(row: Mapping[str, Any]) -> VerifiedInstance:
    """一行原始数据 → `VerifiedInstance`。缺字段当场报错，不填默认值。"""
    try:
        return VerifiedInstance(
            instance_id=str(row["instance_id"]),
            repo=str(row["repo"]),
            base_commit=str(row["base_commit"]),
            patch=str(row["patch"]),
            test_patch=str(row["test_patch"]),
            problem_statement=str(row["problem_statement"]),
            hints_text=str(row.get("hints_text") or ""),
            created_at=str(row.get("created_at") or ""),
            version=str(row.get("version") or ""),
            FAIL_TO_PASS=_test_ids(row["FAIL_TO_PASS"]),
            PASS_TO_PASS=_test_ids(row["PASS_TO_PASS"]),
            environment_setup_commit=str(row["environment_setup_commit"]),
            difficulty=str(row.get("difficulty") or ""),
        )
    except KeyError as exc:
        instance = dict(row).get("instance_id")
        raise SwebenchImportError(f"官方数据行缺字段 {exc}：{instance!r}") from exc


def load_instances(path: Path) -> list[VerifiedInstance]:
    """读 `fetch` 落盘的 JSONL（一行一道题）。"""
    instances = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                instances.append(parse_instance(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise SwebenchImportError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
    return instances


# ══════════════════════════════════════════════════════════════
# 官方镜像与环境
# ══════════════════════════════════════════════════════════════


def official_image(instance_id: str) -> str:
    """`astropy__astropy-12907` → `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`。"""
    slug = instance_id.lower().replace("__", "_1776_")
    return f"{OFFICIAL_IMAGE_NAMESPACE}/sweb.eval.{OFFICIAL_IMAGE_ARCH}.{slug}:{OFFICIAL_IMAGE_TAG}"


def official_environment_id(instance_id: str) -> str:
    """走官方镜像时的环境 id：一道题一个（镜像就是按题发的）。"""
    return f"swebench__{instance_id}"


def bucket_environment_id(instance: VerifiedInstance) -> str:
    """退回自建时的环境 id：按 `environment_setup_commit` 分桶，一桶一个镜像。

    桶名带仓库名是为了人能读；带 12 位 SHA 是因为同一个仓库有几十个桶
    （sphinx 3.0 到 7.2 每个小版本一个）。
    """
    setup = instance.environment_setup_commit[:12]
    return f"swebench__{instance.owner}__{instance.name}__env-{setup}"


def pytest_command_for(repo: str) -> str:
    """按仓库的源码布局拼测试命令（`PYTHONPATH` 指到包所在那一层）。

    **名字不能以 `test_` 开头**：测试文件 import 它之后 pytest 会把它当用例收集
    （`assembly.function_name_of` 的注释里说过同一件事）。
    """
    root = IMPORT_ROOTS[repo]
    pythonpath = f"{WORKSPACE_TARGET}/{root}" if root else WORKSPACE_TARGET
    return _TEST_COMMAND.format(pythonpath=pythonpath)


@dataclass(frozen=True, slots=True)
class EnvironmentBinding:
    """一道题要绑到的环境：既是 `TaskDefinition` 里的执行字段，也是 `environment_specs` 那一行。"""

    environment_id: str
    #: `official` = 官方镜像；`fallback` = 拉不动，退回按桶自建（镜像还没有）。
    kind: str
    image_tag: str | None
    python_version: str
    install_command: str
    test_command: str
    pre_test_command: str | None
    test_report_path: str = DEFAULT_REPORT_PATH
    extra_protected_paths: tuple[str, ...] = ()

    def spec_row(self) -> dict[str, Any]:
        """`cli.queue.upsert_task` 要的环境规格字典。"""
        return {
            "python_version": self.python_version,
            "extra_protected_paths": list(self.extra_protected_paths),
            "image_tag": self.image_tag,
        }


def official_environment(instance: VerifiedInstance, *, python_version: str) -> EnvironmentBinding:
    """绑到官方镜像。`python_version` 是拉镜像时探出来的，不猜。

    `install_command` 在这条路上不会被执行（依赖在镜像里已经装好，ADR-008），
    写的是"这个环境是怎么来的"，给看库的人一个交代。
    """
    return EnvironmentBinding(
        environment_id=official_environment_id(instance.instance_id),
        kind="official",
        image_tag=official_image(instance.instance_id),
        python_version=python_version,
        install_command=(
            f"# 复用官方镜像 {official_image(instance.instance_id)}，依赖已装好；"
            f"env 桶 environment_setup_commit={instance.environment_setup_commit}"
        ),
        test_command=pytest_command_for(instance.repo),
        pre_test_command=PRE_TEST_COMMAND,
    )


def built_environment(
    instance: VerifiedInstance, *, python_version: str, image_tag: str
) -> EnvironmentBinding:
    """绑到**按官方配方本机建**的镜像（`cli.swebench build`，配方改写见 `swebench_recipes`）。

    镜像里的布局和官方一样（`/testbed` + conda 环境 `testbed`），所以测试命令和
    `pre_test_command` 原样复用；区别只在 `kind` 和 tag，报告里要分得开"官方二进制"和"本机建"。
    """
    return EnvironmentBinding(
        environment_id=official_environment_id(instance.instance_id),
        kind="built",
        image_tag=image_tag,
        python_version=python_version,
        install_command=(
            f"# 按官方配方本机建（swebench 的 MAP_REPO_VERSION_TO_SPECS，依赖走清华源），"
            f"镜像 {image_tag}；env 桶 environment_setup_commit={instance.environment_setup_commit}"
        ),
        test_command=pytest_command_for(instance.repo),
        pre_test_command=PRE_TEST_COMMAND,
    )


def fallback_environment(instance: VerifiedInstance) -> EnvironmentBinding:
    """官方镜像拉不动时退回的自建规格（§8.6 第二条）。

    只登记桶和意图，镜像要人按 `images/envs/` 的配方另建 —— 官方的安装步骤
    （swebench 包里 `MAP_REPO_VERSION_TO_SPECS`）不在数据集里，这里不编。
    `python_version` 同理写 `unknown`，别人一眼看得出这行还没落实。
    """
    environment_id = bucket_environment_id(instance)
    return EnvironmentBinding(
        environment_id=environment_id,
        kind="fallback",
        image_tag=f"bench-env:{environment_id}",
        python_version="unknown",
        install_command="python -m pip install -e .",
        # 自建镜像走 `workspace_pth_line()` 把 /workspace 放进 sys.path，不用 PYTHONPATH；
        # 但配方还没写，这里先按官方镜像那套命令留着，建好镜像后由配方决定
        test_command=pytest_command_for(instance.repo),
        pre_test_command=None,
    )


# ══════════════════════════════════════════════════════════════
# 离线筛（不联网、不起容器）
# ══════════════════════════════════════════════════════════════

#: 漏斗第一层的格子。顺序就是判断顺序，一道题只落一格。
BUCKET_OK = "OK"
BUCKET_REPO_NOT_PYTEST = "REPO_NOT_PYTEST"
BUCKET_TEST_PATCH_NON_TEST = "TEST_PATCH_NON_TEST_PATH"
BUCKET_GOLD_PROTECTED = "GOLD_TOUCHES_PROTECTED"
BUCKET_ISSUE_LEAKS = "ISSUE_LEAKS_FIX"
BUCKET_NO_F2P = "NO_F2P"
BUCKET_SCHEMA_REJECTED = "SCHEMA_REJECTED"


@dataclass(frozen=True, slots=True)
class Screened:
    """一道官方题过完离线筛的结论。"""

    instance: VerifiedInstance
    bucket: str
    detail: str = ""
    #: 从 PASS_TO_PASS 里剔掉的、不是用例 ID 的条目（`[100%]` 之类）。
    dropped_p2p: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.bucket == BUCKET_OK


def looks_truncated(test_id: str) -> bool:
    """官方数据里参数化用例 id 是从 pytest 日志按空白切出来的，参数里带空格的就被切断了
    （`test_stem[png-w/` 其实是 `test_stem[png-w/ line collection]`）。这种 id 交给 pytest
    只会得到 "not found"，整场一条都不跑（2026-09-16 实测：50 道里 12 道有，7 道因此
    停在 S4）。判据：方括号没配对。"""
    return test_id.count("[") != test_id.count("]")


def clean_test_ids(ids: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把官方的用例列表分成（能用的, 剔掉的）。剔两种：不像 nodeid 的（`[100%]`）、被切断的。"""
    kept: list[str] = []
    dropped: list[str] = []
    for test_id in ids:
        usable = _NODEID_SEPARATOR in test_id and not looks_truncated(test_id)
        (kept if usable else dropped).append(test_id)
    return tuple(kept), tuple(dropped)


def clean_pass_to_pass(ids: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return clean_test_ids(ids)


def screen(instance: VerifiedInstance) -> Screened:
    """离线筛一道题。**判断顺序固定**，落哪一格就只报那一格。

    前四条和 `TaskDefinition` 构造时的校验是同一套规则，先判一遍是为了给漏斗
    一个能说清楚的格子；最后真的组装一次兜底，pydantic 还拒收的记 `SCHEMA_REJECTED`。
    """
    if instance.repo not in IMPORT_ROOTS:
        why = EXCLUDED_REPOS.get(instance.repo, "不在 IMPORT_ROOTS 名单里，源码布局没核过")
        return Screened(instance, BUCKET_REPO_NOT_PYTEST, why)

    test_paths = derive_patch_paths(instance.test_patch)
    non_test = [p for p in test_paths if not is_protected(p, DEFAULT_PROTECTED_PATTERNS)]
    if non_test:
        return Screened(
            instance, BUCKET_TEST_PATCH_NON_TEST, f"test_patch 改了非测试路径：{non_test}"
        )

    gold_paths = derive_patch_paths(instance.patch)
    hits = protected_hits(tuple(gold_paths), (*DEFAULT_PROTECTED_PATTERNS, *test_paths))
    if hits:
        return Screened(instance, BUCKET_GOLD_PROTECTED, f"gold_patch 命中受保护路径：{hits}")

    if _LEAK_PR_URL.search(instance.problem_statement):
        return Screened(instance, BUCKET_ISSUE_LEAKS, "题面里有指向 PR 的链接")
    if _LEAK_DIFF_BLOCK.search(instance.problem_statement):
        return Screened(instance, BUCKET_ISSUE_LEAKS, "题面里贴了 diff")

    if not instance.FAIL_TO_PASS:
        return Screened(instance, BUCKET_NO_F2P, "FAIL_TO_PASS 是空的")
    if not clean_test_ids(instance.FAIL_TO_PASS)[0]:
        return Screened(instance, BUCKET_NO_F2P, "FAIL_TO_PASS 全是被切断的参数化 id")

    _, dropped = clean_pass_to_pass(instance.PASS_TO_PASS)
    try:
        # 用退回规格试组装：它不需要镜像探测结果，而校验规则和官方规格那条路完全一样
        build_task(instance, fallback_environment(instance))
    except Exception as exc:  # pydantic 的 ValidationError 也在里面
        return Screened(instance, BUCKET_SCHEMA_REJECTED, str(exc).strip()[:300], dropped)
    return Screened(instance, BUCKET_OK, dropped_p2p=dropped)


def screen_all(instances: Iterable[VerifiedInstance]) -> list[Screened]:
    return [screen(instance) for instance in instances]


# ══════════════════════════════════════════════════════════════
# 固定种子分层抽样（§8.6）
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Sample:
    """一次抽样的结果，连同复现它所需的全部参数。"""

    seed: int
    size: int
    #: 抽样池：离线筛通过的全部题号（排好序）。池子变了名单就可能变，所以记它的摘要。
    pool_size: int
    pool_digest: str
    #: 每层配额。
    quotas: dict[str, int]
    #: 抽中的题号，按仓库名排、层内按打乱后的顺序。
    chosen: tuple[str, ...]
    #: 每层没抽中的、按打乱顺序排好的备选（要补题时从头取，不用换种子）。
    replacements: dict[str, tuple[str, ...]]

    def to_json(self) -> dict[str, Any]:
        return {
            "dataset": HF_DATASET,
            "seed": self.seed,
            "size": self.size,
            "pool_size": self.pool_size,
            "pool_digest": self.pool_digest,
            "quotas": dict(self.quotas),
            "chosen": list(self.chosen),
            "replacements": {repo: list(ids) for repo, ids in self.replacements.items()},
        }


def allocate_quotas(counts: Mapping[str, int], size: int) -> dict[str, int]:
    """按层大小成比例分配 `size` 个名额（最大余数法）。

    非空层至少 1 道 —— 分层的意思就是每个仓库都要在样本里出现；名额不够
    每层一道时才退回纯比例。名额多于池子时按池子封顶。
    """
    strata = {repo: n for repo, n in sorted(counts.items()) if n > 0}
    total = sum(strata.values())
    if not strata or size <= 0:
        return dict.fromkeys(strata, 0)
    size = min(size, total)

    floor_each = 1 if size >= len(strata) else 0
    quotas = {repo: min(floor_each, n) for repo, n in strata.items()}
    remaining = size - sum(quotas.values())

    # 剩下的名额按比例分：先取整数部分，再把余数大的层各补 1
    exact = {repo: remaining * n / total for repo, n in strata.items()}
    for repo, value in exact.items():
        quotas[repo] = min(strata[repo], quotas[repo] + int(value))
    leftover = size - sum(quotas.values())
    by_remainder = sorted(strata, key=lambda r: (-(exact[r] - int(exact[r])), r))
    for repo in by_remainder:
        if leftover <= 0:
            break
        if quotas[repo] < strata[repo]:
            quotas[repo] += 1
            leftover -= 1
    # 还有剩（某些层封顶了）就按层名顺序补给没满的层
    for repo in sorted(strata):
        while leftover > 0 and quotas[repo] < strata[repo]:
            quotas[repo] += 1
            leftover -= 1
    return quotas


def _shuffled(ids: Sequence[str], seed: int, repo: str) -> list[str]:
    """层内打乱：种子按 `f"{seed}/{repo}"` 派生，各层互不影响。"""
    ordered = sorted(ids)
    random.Random(f"{seed}/{repo}").shuffle(ordered)
    return ordered


def pool_digest(instance_ids: Iterable[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(sorted(instance_ids)).encode("utf-8")).hexdigest()


def stratified_sample(
    pool: Sequence[VerifiedInstance], *, size: int = DEFAULT_SAMPLE_SIZE, seed: int = DEFAULT_SEED
) -> Sample:
    """从离线筛通过的池子里按仓库分层抽 `size` 道。确定性的：同参数两次结果逐字相同。"""
    by_repo: dict[str, list[str]] = {}
    for instance in pool:
        by_repo.setdefault(instance.repo, []).append(instance.instance_id)
    quotas = allocate_quotas({repo: len(ids) for repo, ids in by_repo.items()}, size)

    chosen: list[str] = []
    replacements: dict[str, tuple[str, ...]] = {}
    for repo in sorted(by_repo):
        order = _shuffled(by_repo[repo], seed, repo)
        take = quotas[repo]
        chosen.extend(order[:take])
        replacements[repo] = tuple(order[take:])
    return Sample(
        seed=seed,
        size=size,
        pool_size=len(pool),
        pool_digest=pool_digest(i.instance_id for i in pool),
        quotas=quotas,
        chosen=tuple(chosen),
        replacements=replacements,
    )


# ══════════════════════════════════════════════════════════════
# 组装
# ══════════════════════════════════════════════════════════════


def _title_of(problem_statement: str) -> str:
    """题面第一行当标题。官方的 `problem_statement` 本来就是"标题 + 正文"拼的。"""
    first = next((line.strip() for line in problem_statement.splitlines() if line.strip()), "")
    if len(first) > MAX_TITLE_CHARS:
        first = first[: MAX_TITLE_CHARS - 1] + "…"
    return first or "(no title)"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _difficulty_tag(label: str) -> str | None:
    """`15 min - 1 hour` → `swebench-difficulty-15-min-1-hour`。"""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"swebench-difficulty-{slug}" if slug else None


def build_task(
    instance: VerifiedInstance,
    environment: EnvironmentBinding,
    *,
    extra_tags: Iterable[str] = (),
) -> TaskDefinition:
    """官方行 + 环境 → `TaskDefinition`。

    构造 `TaskDefinition` 时会把 §7 和协议的规则挨条查一遍（test_patch 只能碰测试文件、
    gold 不能命中受保护路径、题面不能泄题……），不合格的在这里抛 `ValidationError`。
    难度按 §7.8 从 gold 补丁规模派生，和挖掘题同一把尺子；官方的人工难度标注进 tag。
    `extra_tags` 给调用方加标记用（比如 `swebench_overrides` 剔过 P2P 的题）。
    """
    fail_to_pass, _ = clean_test_ids(instance.FAIL_TO_PASS)
    pass_to_pass, _ = clean_pass_to_pass(instance.PASS_TO_PASS)
    tags = {
        "swebench-verified",
        "calibration",
        DATASET_ID,
        f"swebench-version-{instance.version or 'unknown'}",
        f"swebench-env-{instance.environment_setup_commit[:12]}",
        {
            "official": "official-image",
            "built": "official-recipe-local-build",
        }.get(environment.kind, "fallback-env"),
        *extra_tags,
    }
    difficulty_tag = _difficulty_tag(instance.difficulty)
    if difficulty_tag:
        tags.add(difficulty_tag)

    return TaskDefinition(
        task_id=instance.instance_id,
        dataset_id=DATASET_ID,
        repo_url=f"https://github.com/{instance.repo}.git",
        repo_name=instance.repo,
        base_commit=instance.base_commit,
        environment_id=environment.environment_id,
        issue_title=_title_of(instance.problem_statement),
        issue_body=instance.problem_statement,
        # SWE-bench Verified 全是英文 issue。不做语言检测：§8.5 要的是如实标注
        issue_language=IssueLanguage.EN,
        # 官方 hints_text 是修复前的 PR 讨论，可能带答案；对齐 SWE-bench Verified 的做法不给
        hints_text=None,
        install_command=environment.install_command,
        pre_test_command=environment.pre_test_command,
        test_command=environment.test_command,
        test_report_path=environment.test_report_path,
        test_patch=instance.test_patch,
        fail_to_pass=list(fail_to_pass),
        pass_to_pass=list(pass_to_pass),
        # 官方 P2P 是"测试补丁碰到的文件里、修复前后都通过的用例"，不是我们 §7.7 的抽样，
        # 所以不记 p2p_sampling
        p2p_sampling=None,
        gold_patch=instance.patch,
        test_timeout_s=OFFICIAL_TEST_TIMEOUT_S,
        source_pr_url=f"https://github.com/{instance.repo}/pull/{instance.pr_number}",
        created_at_upstream=_parse_time(instance.created_at),
        language="python",
        framework=instance.name,
        difficulty=derive_difficulty(instance.patch),
        tags=sorted(tags),
    )


# ══════════════════════════════════════════════════════════════
# 漏斗
# ══════════════════════════════════════════════════════════════


@dataclass
class Funnel:
    """导入漏斗每一层的计数（AC 8）。后几层由 CLI 从库里和缓存里读出来填。"""

    official_total: int = 0
    offline: Counter[str] = field(default_factory=Counter)
    sampled: int = 0
    image_pulled: int = 0
    image_unavailable: list[str] = field(default_factory=list)
    mirror_ready: int = 0
    mirror_failed: list[str] = field(default_factory=list)
    imported: int = 0
    validation: Counter[str] = field(default_factory=Counter)
    per_repo: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def pool(self) -> int:
        return self.offline.get(BUCKET_OK, 0)

    @property
    def valid(self) -> int:
        return self.validation.get("VALID", 0)


def render_funnel(funnel: Funnel) -> str:
    """漏斗表，Markdown。直接贴进 `03-benchmark-spec.md` 的落地实录。"""
    lines = [
        "| 层 | 数量 | 说明 |",
        "|:---|---:|:---|",
        f"| 官方题数 | {funnel.official_total} | {HF_DATASET} |",
    ]
    for bucket, count in sorted(funnel.offline.items(), key=lambda kv: (-kv[1], kv[0])):
        if bucket == BUCKET_OK:
            continue
        lines.append(f"| 离线筛掉：{bucket} | {count} | |")
    lines.append(f"| 离线筛通过（抽样池） | {funnel.pool} | |")
    lines.append(f"| 抽样后 | {funnel.sampled} | |")
    unavailable = "、".join(funnel.image_unavailable) if funnel.image_unavailable else ""
    lines.append(f"| 官方镜像拉得到 | {funnel.image_pulled} | 拉不到：{unavailable or '无'} |")
    failed = "、".join(funnel.mirror_failed) if funnel.mirror_failed else ""
    lines.append(f"| git 镜像备好 | {funnel.mirror_ready} | 失败：{failed or '无'} |")
    lines.append(f"| 入库 | {funnel.imported} | |")
    for state, count in sorted(funnel.validation.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| 八步验证：{state} | {count} | |")
    lines.append(f"| **VALID** | **{funnel.valid}** | |")
    if funnel.per_repo:
        lines.append("")
        lines.append("| 仓库 | 池 | 抽中 | VALID |")
        lines.append("|:---|---:|---:|---:|")
        for repo, counts in sorted(funnel.per_repo.items()):
            lines.append(
                f"| {repo} | {counts.get('pool', 0)} | {counts.get('sampled', 0)} "
                f"| {counts.get('valid', 0)} |"
            )
    return "\n".join(lines)


__all__ = [
    "DATASET_ID",
    "DEFAULT_SAMPLE_SIZE",
    "DEFAULT_SEED",
    "EXCLUDED_REPOS",
    "HF_DATASET",
    "IMPORT_ROOTS",
    "OFFICIAL_ENV_PYTHON",
    "OFFICIAL_TESTBED",
    "OFFICIAL_TEST_TIMEOUT_S",
    "PRE_TEST_COMMAND",
    "EnvironmentBinding",
    "Funnel",
    "Sample",
    "Screened",
    "SwebenchImportError",
    "VerifiedInstance",
    "allocate_quotas",
    "bucket_environment_id",
    "build_task",
    "built_environment",
    "clean_pass_to_pass",
    "fallback_environment",
    "load_instances",
    "official_environment",
    "official_environment_id",
    "official_image",
    "parse_instance",
    "pool_digest",
    "pytest_command_for",
    "render_funnel",
    "screen",
    "screen_all",
    "stratified_sample",
]
