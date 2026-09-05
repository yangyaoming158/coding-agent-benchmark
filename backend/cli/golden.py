"""Golden Tasks：手写的验证基石（E1-T2）。

    python -m cli.golden build            # 从 sources/ 生成任务 JSON 和本地镜像
    python -m cli.golden build --check    # CI 用：生成结果和仓库里的 JSON 不一致就非零退出
    python -m cli.golden verify           # 六步验证，逐题逐步打印结论
    python -m cli.golden list             # 列出有哪些题

## 这是什么

四道**手写**的评测题。它们不来自 GitHub 挖掘，不依赖网络，跑完只要几秒钟。
Week 1 的内核开发全靠它们：判定引擎、沙箱、Runner 适配器都需要一批"已知答案"
的题目来自测，而真实题目要等挖掘器（E1-T4）就位。

每道题都是一个真实会犯的错：字符串扫描漏了引号、布尔表达式短路、取整方式选错、
边界值没校验。issue 用中文写，长度和信息量按真实 issue 的样子来。

## 源码怎么组织

    datasets/golden/
        <task_id>.json          生成物：完整的 TaskDefinition
        environments/           生成物：一个环境规格一个文件
        sources/<task_id>/
            task.toml     元数据：F2P / P2P 用例、难度、标签、环境规格
            issue.md      交给被测 AI 的 issue（第一行 `# 标题`，其余是正文）
            base/         有 bug 的完整文件树，含仓库原有的测试
            fix/          修复 PR 改动的文件（源码修复 + 新增的测试），覆盖到 base 上

**`fix/` 里源码和测试是混在一起的，这是刻意的**——真实世界里一个修复 PR 就是这样，
既改代码又加测试。`build` 按受保护路径规则把这个 PR 的 diff 劈成两半：
碰测试文件的部分是 `test_patch`，其余是 `gold_patch`。这和 SWE-bench 从真实 PR
派生任务的做法完全一致，也顺带保证了两个补丁一定能干净地打上去。

## 上游仓库是生成出来的

Golden 题没有真实上游，`build` 会用 `base/` 和 `fix/` 造一个两提交的仓库，
再 `git clone --mirror` 到 `var/mirrors/` 下。提交人和时间都是写死的常量，
所以 `base_commit` 在任何机器上都一样——这是把 `base_commit` 写进版本库里的 JSON
的前提。

`repo_url` 记成 `golden://<owner>/<repo>`，明确表示"这个仓库是生成的，没有上游"。
换机器之后 `var/mirrors/` 是空的（它在 .gitignore 里），跑一次 `make golden` 就有了。

## 六步验证

`verify` 跑的是这六步，对应 §7.2(5)(6) 和协议 C-64：

    1. 物化      工作区历史只有一个提交，树哈希等于 base 树
    2. 补丁体检   gold_patch 不碰受保护路径；test_patch 只碰测试文件；两者都能打上
    3. F2P 全挂   base + test_patch 上，每条 F2P 都必须失败      ← Noop 解决率 0% 的依据
    4. P2P 全过   base + test_patch 上，P2P 必须全部通过
    5. F2P 全过   base + test_patch + gold_patch 上，F2P 必须全部通过 ← Oracle 100% 的依据
    6. P2P 仍全过 同上状态，P2P 不能被 gold_patch 打挂

**这里跑测试是在本机直接起 pytest 子进程，不是 E4-T2 的沙箱执行器。** 两者目的不同：
这里要的是"这批题自己站得住"，跑的是我们自己写的代码，没有不可信输入；
真正的评测必须进容器，那是 E2-T2 / E4-T2 的事。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.benchmark.schema import P2PSampling, TaskDefinition
from app.domain.enums import IssueLanguage, TaskDifficulty
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import DEFAULT_PROTECTED_PATTERNS, is_protected
from app.sandbox.git_cli import run_git
from app.sandbox.mirror import MirrorManager, mirror_dir_name
from app.sandbox.workspace import Workspace, materialize_workspace

#: 仓库根。层级：cli/golden.py → cli → backend → 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "datasets" / "golden"
SOURCES_DIR = GOLDEN_ROOT / "sources"
#: 环境规格，一个规格一个文件。字段和 `environment_specs` 表的列对齐，E1-T3 / E8 入库时直接用。
#:
#: 放在子目录里而不是和任务 JSON 挤在一起：`datasets/golden/*.json` 这个通配符
#: 是 `cli.task import` 的标准用法，混进一个不是任务的 JSON，导入命令会当场拒收它。
ENVIRONMENTS_DIR = GOLDEN_ROOT / "environments"
DEFAULT_MIRROR_ROOT = REPO_ROOT / "var" / "mirrors"

#: 造上游仓库时的固定身份与时间。
#:
#: 写死是硬性要求，不是讲究：`base_commit` 要写进版本库里的任务 JSON，
#: 而 commit SHA 里含作者和提交时间。用真实时间的话，换台机器重新 build 一次，
#: 每道题的 base_commit 都会变，`build --check` 永远红。
UPSTREAM_AUTHOR_NAME = "bench-golden"
UPSTREAM_AUTHOR_EMAIL = "golden@bench.local"
BASE_COMMIT_DATE = "2026-01-05T09:00:00+00:00"
FIX_COMMIT_DATE = "2026-01-12T09:00:00+00:00"
BASE_COMMIT_MESSAGE = "初始版本"
FIX_COMMIT_MESSAGE = "修复问题并补上回归测试"

#: 环境规格的默认值。task.toml 的 [environment] 段可以逐项覆盖。
#:
#: `test_command` 按 §7.2(4) 的四条硬性要求来：出机器可解析的报告（junitxml）、
#: 关掉随机顺序和缓存插件、能接用例 ID 列表、不带 `-x`。
DEFAULT_ENVIRONMENT: dict[str, Any] = {
    "python_version": "3.11",
    # Golden 题不依赖任何第三方库，容器里只需要 pytest 本身
    "install_command": "python -m pip install pytest",
    "pre_test_command": None,
    "test_command": (
        "python -m pytest -p no:cacheprovider -p no:randomly --junitxml=report/junit.xml"
    ),
    "test_framework": "pytest",
    "test_report_path": "report/junit.xml",
    "extra_protected_paths": [],
}

#: Golden 题的测试预算。E1-T2 的目标里写着"可在 60 秒内跑完"，这里把它变成硬约束：
#: 哪天有人往 golden 里塞了一道慢题，评测会超时，而不是悄悄拖长每一轮自测。
GOLDEN_TEST_TIMEOUT_S = 60
GOLDEN_AGENT_TIMEOUT_S = 300

#: pytest 的退出码里我们关心的三种。
PYTEST_OK = 0
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS = 5


class GoldenError(RuntimeError):
    """Golden 数据集本身有问题（源码目录写错、验证不通过）。"""


# ── 读源码目录 ──────────────────────────────────────────────


@dataclass(frozen=True)
class GoldenSource:
    """一道 golden 题的源码目录。"""

    task_id: str
    path: Path
    #: `task.toml` 原样读出来的内容。tomllib 给的就是 `dict[str, Any]`，
    #: 取值一律走下面几个带校验的取数函数，不要直接 `meta["x"]` 再 cast。
    meta: dict[str, Any]
    issue_title: str
    issue_body: str

    @property
    def base_dir(self) -> Path:
        return self.path / "base"

    @property
    def fix_dir(self) -> Path:
        return self.path / "fix"

    @property
    def repo_name(self) -> str:
        return require_str(self.meta, "repo_name", self.task_id)

    @property
    def environment(self) -> dict[str, Any]:
        """合并了默认值的环境规格。"""
        merged: dict[str, Any] = dict(DEFAULT_ENVIRONMENT)
        override = self.meta.get("environment", {})
        if not isinstance(override, dict):
            raise GoldenError(f"{self.task_id} 的 [environment] 不是一个表")
        merged.update(override)
        merged["environment_id"] = environment_id_for(self.repo_name, str(merged["python_version"]))
        merged["repo_name"] = self.repo_name
        return merged


def require_str(meta: Mapping[str, Any], key: str, task_id: str) -> str:
    """取一个必填的字符串字段。类型不对就报清楚是哪道题的哪个键。

    TOML 是手写的，写错类型（比如把 `difficulty` 写成数字）很常见。
    不校验的话错误会一路飘到 Pydantic 那里，报的是"字段类型不对"，
    不会告诉你是 `sources/xxx/task.toml` 里写错了。
    """
    value = meta.get(key)
    if not isinstance(value, str) or not value:
        raise GoldenError(f"{task_id} 的 task.toml 里 {key} 必须是非空字符串，实际是 {value!r}")
    return value


def string_list(meta: Mapping[str, Any], key: str, task_id: str) -> list[str]:
    """取一个字符串列表字段，排序去重。缺省当空列表。"""
    value = meta.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GoldenError(f"{task_id} 的 task.toml 里 {key} 必须是字符串列表，实际是 {value!r}")
    return sorted(set(value))


def environment_id_for(repo_name: str, python_version: str) -> str:
    """`bench-golden/textkit` + `3.11` → `bench-golden__textkit__py311`。

    人可读，和 `environment_specs.environment_id` 那一列的注释里给的样子一致。
    """
    return f"{repo_name.replace('/', '__')}__py{python_version.replace('.', '')}"


def load_source(path: Path) -> GoldenSource:
    """读一个源码目录。缺东西就报清楚缺哪个文件。"""
    for required in ("task.toml", "issue.md", "base", "fix"):
        if not (path / required).exists():
            raise GoldenError(f"{path} 里缺 {required}")

    meta = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
    title, body = parse_issue(path / "issue.md")
    task_id = require_str(meta, "task_id", path.name)
    if task_id != path.name:
        raise GoldenError(f"目录名 {path.name} 和 task.toml 里的 task_id {task_id} 对不上")
    return GoldenSource(task_id=task_id, path=path, meta=meta, issue_title=title, issue_body=body)


def parse_issue(path: Path) -> tuple[str, str]:
    """`issue.md` 拆成标题和正文。第一行必须是 `# 标题`。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise GoldenError(f"{path} 的第一行必须是 `# 标题`")
    return lines[0][2:].strip(), "\n".join(lines[1:]).strip() + "\n"


def iter_sources(only: Sequence[str] = ()) -> Iterator[GoldenSource]:
    """按 task_id 排序遍历所有源码目录。`only` 非空时只要这几道。"""
    if not SOURCES_DIR.is_dir():
        raise GoldenError(f"源码目录不存在：{SOURCES_DIR}")
    for path in sorted(p for p in SOURCES_DIR.iterdir() if p.is_dir()):
        if only and path.name not in only:
            continue
        yield load_source(path)


# ── 造上游仓库 ──────────────────────────────────────────────


def _commit_env(date: str) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": UPSTREAM_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": UPSTREAM_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": UPSTREAM_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": UPSTREAM_AUTHOR_EMAIL,
        "GIT_COMMITTER_DATE": date,
    }


def _overlay(source_dir: Path, dest: Path) -> None:
    """把 `source_dir` 的内容盖到 `dest` 上，同名文件覆盖。"""
    shutil.copytree(source_dir, dest, dirs_exist_ok=True)


def build_upstream(source: GoldenSource, dest: Path) -> tuple[str, str]:
    """在 `dest` 造出这道题的上游仓库，返回 (base_commit, fix_commit)。

    历史是两个提交：base（有 bug）→ fix（修复 PR）。和真实仓库一样，
    修复提交里既有源码改动也有新测试——`split_patches()` 再把它劈成两半。
    """
    dest.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--quiet", "--initial-branch=main"], cwd=dest)

    _overlay(source.base_dir, dest)
    run_git(["add", "--all", "--force", "--", "."], cwd=dest)
    run_git(
        ["commit", "--quiet", "--no-verify", "--message", BASE_COMMIT_MESSAGE],
        cwd=dest,
        env_extra=_commit_env(BASE_COMMIT_DATE),
    )
    base_commit = run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()

    _overlay(source.fix_dir, dest)
    run_git(["add", "--all", "--force", "--", "."], cwd=dest)
    run_git(
        ["commit", "--quiet", "--no-verify", "--message", FIX_COMMIT_MESSAGE],
        cwd=dest,
        env_extra=_commit_env(FIX_COMMIT_DATE),
    )
    fix_commit = run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()

    if base_commit == fix_commit:
        raise GoldenError(f"{source.task_id} 的 fix/ 没有改动任何文件，这道题没有修复内容")
    return base_commit, fix_commit


def split_patches(repo: Path, base_commit: str, fix_commit: str) -> tuple[str, str]:
    """把修复提交的 diff 按受保护路径劈成 (test_patch, gold_patch)。

    劈的依据就是 C-42 的受保护路径清单：碰了测试文件的算测试补丁，其余算官方补丁。
    这和平台过滤被测 AI 补丁用的是同一份规则，所以不会出现"这里算测试、那里算源码"
    的两套口径。
    """
    changed = _changed_paths(repo, base_commit, fix_commit)
    test_paths = [p for p in changed if is_protected(p, DEFAULT_PROTECTED_PATTERNS)]
    source_paths = [p for p in changed if not is_protected(p, DEFAULT_PROTECTED_PATTERNS)]

    if not test_paths:
        raise GoldenError(f"{repo.name} 的 fix/ 没有加或改任何测试文件，F2P 无从谈起")
    if not source_paths:
        raise GoldenError(f"{repo.name} 的 fix/ 只改了测试，没有修复内容（§7.2 坏任务规则）")

    return (
        _diff(repo, base_commit, fix_commit, test_paths),
        _diff(repo, base_commit, fix_commit, source_paths),
    )


def _changed_paths(repo: Path, base_commit: str, fix_commit: str) -> list[str]:
    out = run_git(
        ["diff", "--name-only", "-z", "--no-renames", base_commit, fix_commit], cwd=repo
    ).stdout
    return sorted(item for item in out.split("\0") if item)


def _diff(repo: Path, base_commit: str, fix_commit: str, paths: Sequence[str]) -> str:
    """生成一段 unified diff。

    每个影响输出格式的开关都显式写出来，不吃 git 的默认值：这段 diff 会进版本库里的
    任务 JSON，还参与 `content_hash`。哪天换个 git 版本、或者谁的配置里开了
    `diff.noprefix`，输出变一个字节，`build --check` 就红了，而题目其实什么都没改。
    """
    return (
        run_git(
            [
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                "--unified=3",
                "--diff-algorithm=myers",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                base_commit,
                fix_commit,
                "--",
                *paths,
            ],
            cwd=repo,
        ).stdout
        + "\n"
    )


# ── 组装任务 JSON ───────────────────────────────────────────


def assemble_task(
    source: GoldenSource, base_commit: str, test_patch: str, gold_patch: str
) -> TaskDefinition:
    """把源码目录 + 生成的补丁拼成一个 `TaskDefinition`。

    构造时 `TaskDefinition` 会把 §7 和协议的规则挨条查一遍（issue 泄题、
    gold_patch 碰受保护路径、F2P 与 P2P 重叠……），所以这一步同时就是校验。
    """
    meta = source.meta
    task_id = source.task_id
    pass_to_pass = string_list(meta, "pass_to_pass", task_id)
    fail_to_pass = string_list(meta, "fail_to_pass", task_id)
    environment = source.environment
    owner, _, repo = source.repo_name.partition("/")
    framework = meta.get("framework")

    return TaskDefinition(
        task_id=source.task_id,
        dataset_id="golden-v1",
        # golden:// 是刻意造的假 scheme：这个仓库是 build 出来的，没有上游可拉。
        # 写一个像模像样的 https 地址反而危险——有人会真的去 clone。
        repo_url=f"golden://{owner}/{repo}",
        repo_name=source.repo_name,
        base_commit=base_commit,
        environment_id=str(environment["environment_id"]),
        issue_title=source.issue_title,
        issue_body=source.issue_body,
        issue_language=IssueLanguage.ZH,
        install_command=str(environment["install_command"]),
        pre_test_command=environment["pre_test_command"],
        test_command=str(environment["test_command"]),
        test_framework=environment["test_framework"],
        test_report_path=str(environment["test_report_path"]),
        test_patch=test_patch,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        # 套件一共就这么几条用例，全跑得起，不需要抽样。
        # strategy=full 时候选池必须等于选中数，这一条由模型自己校验。
        p2p_sampling=P2PSampling(strategy="full", seed=None, total_pool=len(pass_to_pass)),
        gold_patch=gold_patch,
        agent_timeout_s=GOLDEN_AGENT_TIMEOUT_S,
        test_timeout_s=GOLDEN_TEST_TIMEOUT_S,
        difficulty=TaskDifficulty(require_str(meta, "difficulty", task_id)),
        tags=string_list(meta, "tags", task_id),
        language="python",
        # framework = "none" 是 TOML 里表示"没有框架"的写法（TOML 没有 null）
        framework=(None if framework in (None, "none") else str(framework)),
    )


def render_task_json(task: TaskDefinition) -> str:
    return json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def render_environment(environment: Mapping[str, Any]) -> str:
    return json.dumps(dict(environment), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ── build ──────────────────────────────────────────────────


@dataclass
class BuildOutput:
    """一次 build 的产物。`--check` 只比对，不落盘。"""

    tasks: dict[str, str] = field(default_factory=dict)
    environments: list[dict[str, Any]] = field(default_factory=list)
    base_commits: dict[str, str] = field(default_factory=dict)


def build(only: Sequence[str] = (), *, mirror_root: Path | None = None) -> BuildOutput:
    """从源码目录生成任务 JSON 和本地镜像。

    镜像每次都重建。重建比"存在就跳过"安全：源码目录改了而镜像没跟着变的话，
    `base_commit` 会指向一个和当前源码对不上的树，而且不会有任何报错。
    """
    mirror_root = mirror_root or DEFAULT_MIRROR_ROOT
    output = BuildOutput()
    mirrors = MirrorManager(mirror_root)

    for source in iter_sources(only):
        with tempfile.TemporaryDirectory(prefix="golden-upstream-") as tmp:
            upstream = Path(tmp) / "upstream"
            base_commit, fix_commit = build_upstream(source, upstream)
            test_patch, gold_patch = split_patches(upstream, base_commit, fix_commit)
            _refresh_mirror(mirrors, source.repo_name, upstream)

        task = assemble_task(source, base_commit, test_patch, gold_patch)
        output.tasks[source.task_id] = render_task_json(task)
        output.environments.append(source.environment)
        output.base_commits[source.task_id] = base_commit

    output.environments.sort(key=lambda env: str(env["environment_id"]))
    return output


def _refresh_mirror(mirrors: MirrorManager, repo_name: str, upstream: Path) -> None:
    """把生成好的上游仓库镜像到 `var/mirrors/` 下，已有的先删掉。"""
    target = mirrors.path_for(repo_name)
    if target.exists():
        # 删之前确认名字确实是镜像目录名，别因为传错参数把别的目录端了
        if target.name != mirror_dir_name(repo_name):
            raise GoldenError(f"拒绝删除 {target}：它不像是 {repo_name} 的镜像目录")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--mirror", "--quiet", "--", str(upstream), str(target)])


def _expected_files(output: BuildOutput) -> dict[Path, str]:
    """这次 build 应该产出哪些文件、内容分别是什么。写盘和比对共用它。"""
    files = {GOLDEN_ROOT / f"{task_id}.json": text for task_id, text in output.tasks.items()}
    for environment in output.environments:
        path = ENVIRONMENTS_DIR / f"{environment['environment_id']}.json"
        files[path] = render_environment(environment)
    return files


def write_build(output: BuildOutput) -> list[Path]:
    """把 build 结果落盘，返回写了哪些文件。"""
    GOLDEN_ROOT.mkdir(parents=True, exist_ok=True)
    ENVIRONMENTS_DIR.mkdir(parents=True, exist_ok=True)
    written = sorted(_expected_files(output))
    for path, text in _expected_files(output).items():
        path.write_text(text, encoding="utf-8")
    return written


def check_build(output: BuildOutput) -> list[str]:
    """比对 build 结果和仓库里已有的文件，返回对不上的说明。

    多出来的文件也算问题：删掉一道题的 sources 目录却忘了删它的 JSON，
    那份孤儿 JSON 还会被 `load_tasks()` 读进来，看着像题库里还有这道题。
    """
    expected = _expected_files(output)
    problems = []
    for path, text in sorted(expected.items()):
        if not path.exists():
            problems.append(f"{path.name} 不存在")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{path.name} 和 sources/ 对不上")

    on_disk = set(GOLDEN_ROOT.glob("*.json")) | set(ENVIRONMENTS_DIR.glob("*.json"))
    for orphan in sorted(on_disk - set(expected)):
        problems.append(f"{orphan.name} 在 sources/ 里没有对应的源码目录")
    return problems


def load_environments() -> list[dict[str, Any]]:
    """读版本库里已生成的环境规格，按 environment_id 排序。"""
    specs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(ENVIRONMENTS_DIR.glob("*.json"))
    ]
    return sorted(specs, key=lambda env: str(env["environment_id"]))


def load_tasks() -> list[TaskDefinition]:
    """读版本库里已生成的任务 JSON。"""
    return [
        TaskDefinition.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(GOLDEN_ROOT.glob("*.json"))
    ]


# ── 六步验证 ────────────────────────────────────────────────


@dataclass(frozen=True)
class StepResult:
    """一步验证的结论。"""

    number: int
    name: str
    passed: bool
    detail: str


def verify_task(task: TaskDefinition, *, mirror_root: Path | None = None) -> list[StepResult]:
    """对一道题跑六步验证，返回每一步的结论。"""
    mirrors = MirrorManager(mirror_root or DEFAULT_MIRROR_ROOT)
    mirror = mirrors.path_for(task.repo_name)
    if not mirrors.exists(task.repo_name):
        raise GoldenError(f"镜像不存在：{mirror}。先跑 `python -m cli.golden build`")

    with tempfile.TemporaryDirectory(prefix="golden-verify-") as tmp:
        root = Path(tmp)
        steps = [_step_materialize(task, mirror, root / "probe")]

        agent_ws = materialize_workspace(
            mirror_path=mirror, base_commit=task.base_commit, dest=root / "noop"
        )
        oracle_ws = materialize_workspace(
            mirror_path=mirror, base_commit=task.base_commit, dest=root / "oracle"
        )
        steps.append(_step_patch_hygiene(task, agent_ws, root))

        # Noop 侧：只打测试补丁，等价于"被测 AI 交了个空补丁"
        apply_patch(agent_ws, root / "test.patch", task.test_patch)
        steps.append(_step_f2p_all_fail(task, agent_ws))
        steps.append(
            _step_all_pass(agent_ws, 4, "P2P 在 base + test_patch 上全过", task.pass_to_pass)
        )

        # Oracle 侧：先打官方补丁，再打测试补丁（顺序同协议 C-14 的第 2 步和第 4 步）
        apply_patch(oracle_ws, root / "gold.patch", task.gold_patch)
        apply_patch(oracle_ws, root / "test2.patch", task.test_patch)
        steps.append(
            _step_all_pass(oracle_ws, 5, "F2P 在 base + gold + test 上全过", task.fail_to_pass)
        )
        steps.append(
            _step_all_pass(oracle_ws, 6, "P2P 在 base + gold + test 上仍全过", task.pass_to_pass)
        )
    return steps


def _step_materialize(task: TaskDefinition, mirror: Path, dest: Path) -> StepResult:
    workspace = materialize_workspace(mirror_path=mirror, base_commit=task.base_commit, dest=dest)
    count = workspace.commit_count()
    return StepResult(
        1,
        "物化：历史只剩一个提交",
        count == 1,
        f"{workspace.file_count} 个文件，提交数 {count}，树 {workspace.tree_sha[:12]}",
    )


def _step_patch_hygiene(task: TaskDefinition, workspace: Workspace, tmp: Path) -> StepResult:
    """补丁体检：路径归属对不对、能不能干净地打上去。

    `TaskDefinition` 构造时已经查过路径归属（C-64、§7.1），这里重查一遍不是多余：
    验证报告要拿得出证据，不能只说"模型没报错"。能不能打上则是构造时查不到的。
    """
    gold_paths = derive_patch_paths(task.gold_patch)
    protected_in_gold = [p for p in gold_paths if is_protected(p, DEFAULT_PROTECTED_PATTERNS)]
    non_test_in_test_patch = [
        p for p in task.test_patch_paths if not is_protected(p, DEFAULT_PROTECTED_PATTERNS)
    ]
    appliable = patch_applies(workspace, tmp / "check-gold.patch", task.gold_patch) and (
        patch_applies(workspace, tmp / "check-test.patch", task.test_patch)
    )
    passed = not protected_in_gold and not non_test_in_test_patch and appliable
    detail = (
        f"gold 改 {len(gold_paths)} 个文件、test 改 {len(task.test_patch_paths)} 个文件，"
        f"都能 git apply"
        if passed
        else f"受保护路径混进 gold={protected_in_gold}，"
        f"非测试文件混进 test_patch={non_test_in_test_patch}，可打上={appliable}"
    )
    return StepResult(2, "补丁体检：路径归属与可应用性", passed, detail)


def _step_f2p_all_fail(task: TaskDefinition, workspace: Workspace) -> StepResult:
    """每条 F2P 在 base + test_patch 上都必须失败。

    逐条跑而不是一次跑完：一次跑只能得出"至少挂了一条"，而这一步要证明的是
    **每一条**都挂。漏掉一条在 base 上就通过的 F2P，Noop 哨兵就会给出非零解决率。
    """
    passing = []
    missing = []
    for case in task.fail_to_pass:
        code = run_pytest(workspace, [case])
        if code == PYTEST_OK:
            passing.append(case)
        elif code in (PYTEST_USAGE_ERROR, PYTEST_NO_TESTS):
            missing.append(case)
    problems = []
    if passing:
        problems.append(f"这些 F2P 在修复前就已经通过：{passing}")
    if missing:
        problems.append(f"这些 F2P 用例根本不存在：{missing}")
    return StepResult(
        3,
        "F2P 在 base + test_patch 上全挂",
        not problems,
        "、".join(problems) if problems else f"{len(task.fail_to_pass)} 条 F2P 全部失败",
    )


def _step_all_pass(workspace: Workspace, number: int, name: str, cases: list[str]) -> StepResult:
    """一批用例必须全部通过。

    这里可以一次跑完：pytest 只有在**全部**通过时才返回 0，任何一条挂了、
    或者任何一个用例 ID 不存在（退出码 4），都不是 0。
    """
    code = run_pytest(workspace, cases)
    if code == PYTEST_OK:
        return StepResult(number, name, True, f"{len(cases)} 条全部通过")
    if code in (PYTEST_USAGE_ERROR, PYTEST_NO_TESTS):
        return StepResult(number, name, False, f"用例 ID 里有找不到的（pytest 退出码 {code}）")
    return StepResult(number, name, False, f"有用例没通过（pytest 退出码 {code}）")


def run_pytest(workspace: Workspace, cases: Sequence[str]) -> int:
    """在工作区里跑指定用例，返回 pytest 的退出码。

    公开接口：E3-T2 的哨兵验证也用它。里面那套环境变量是**确定性的一部分**，
    各处自己写一份的话，哪天有人漏了 `PYTHONHASHSEED`，同一个补丁两次判定
    可能得到不同结果，而且没人看得出来是从哪来的。

    用 `python -m pytest` 而不是 `pytest`：前者会把当前工作目录放进 sys.path，
    工作区里的 `textkit`、`auth` 这些包才 import 得到。换成 `pytest` 会以
    "ModuleNotFoundError" 收场，而且看起来像题目坏了。
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-q",
            "--no-header",
            *cases,
        ],
        cwd=workspace.path,
        capture_output=True,
        timeout=GOLDEN_TEST_TIMEOUT_S,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(workspace.path),
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            # 确定性三件套：哈希种子固定、不写 .pyc、不让用户的 PYTHONPATH 混进来
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return completed.returncode


def apply_patch(workspace: Workspace, patch_file: Path, patch: str) -> None:
    patch_file.write_text(patch, encoding="utf-8")
    run_git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=workspace.path)


def patch_applies(workspace: Workspace, patch_file: Path, patch: str) -> bool:
    patch_file.write_text(patch, encoding="utf-8")
    result = run_git(
        ["apply", "--check", "--whitespace=nowarn", str(patch_file)],
        cwd=workspace.path,
        check=False,
    )
    return result.returncode == 0


# ── 命令 ────────────────────────────────────────────────────


def cmd_build(args: argparse.Namespace) -> int:
    output = build(args.only)
    if args.check:
        problems = check_build(output)
        if problems:
            print("生成结果和仓库里的文件对不上：", file=sys.stderr)
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            print("\n改了 datasets/golden/sources/ 就要重新生成：make golden", file=sys.stderr)
            return 1
        print(f"{len(output.tasks)} 道题的 JSON 都是最新的")
        return 0

    for path in write_build(output):
        print(f"已写出 {path.relative_to(REPO_ROOT)}")
    for task_id, base_commit in sorted(output.base_commits.items()):
        print(f"  {task_id}  base_commit={base_commit}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    tasks = [t for t in load_tasks() if not args.only or t.task_id in args.only]
    if not tasks:
        print("没有题可验，先跑 `python -m cli.golden build`", file=sys.stderr)
        return 1

    failed = 0
    for task in tasks:
        steps = verify_task(task)
        bad = [s for s in steps if not s.passed]
        failed += bool(bad)
        print(f"\n{'✗' if bad else '✓'} {task.task_id}  ({task.difficulty})")
        for step in steps:
            print(f"    {'✓' if step.passed else '✗'} {step.number}. {step.name} —— {step.detail}")

    total = len(tasks)
    print(f"\n共 {total} 道，六步全过 {total - failed}，有问题 {failed}")
    if not failed:
        print("Oracle 解决率 100%（第 5、6 步）；Noop 解决率 0%（第 3 步）")
    return 1 if failed else 0


def cmd_list(_args: argparse.Namespace) -> int:
    for task in load_tasks():
        print(
            f"{task.task_id:32} {task.difficulty:7} "
            f"F2P {len(task.fail_to_pass):2}  P2P {len(task.pass_to_pass):2}  "
            f"{task.issue_title}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli.golden", description="Golden Tasks 的生成与验证（E1-T2）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="从 sources/ 生成任务 JSON 和本地镜像")
    p_build.add_argument("--check", action="store_true", help="只比对不写；对不上就非零退出")
    p_build.add_argument("--only", nargs="*", default=[], help="只处理这几个 task_id")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify", help="六步验证")
    p_verify.add_argument("--only", nargs="*", default=[], help="只验这几个 task_id")
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser("list", help="列出所有题")
    p_list.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except GoldenError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
