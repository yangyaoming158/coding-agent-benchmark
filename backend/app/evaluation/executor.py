"""测试执行器（E4-T2，协议 C-14 的第 1–6 步）。

一句话：**拿一份干净代码，打上 AI 的补丁，把它对测试文件的手脚全部抹掉，再跑测试。**

    1. 从 base commit 重新导出干净代码       ← 不复用 AI 用过的那份（C-15）
    2. 打上 AI 的补丁                        失败 → PATCH_APPLY_FAILED
    3. 强制还原受保护路径                     ← 防作弊第二道防线（C-16、C-63）
    4. 打上官方测试补丁                       失败 → TEST_DISCOVERY_ERROR
    5. 容器里跑 F2P + P2P 指定的用例，断网    → TEST_TIMEOUT / OOM_KILLED / SANDBOX_ERROR
    6. 解析报告                              → ParsedReport

第 7、8 步（按 C-08 判定、逐条入库）是 E4-T3 的事，这里只把证据备齐。

## 为什么第 1 步要重新导出

被测 AI 在自己的工作目录里可能装了包、改了配置、留了临时文件。直接拿它那份跑测试，
判定结果会被这些东西影响，不同 AI 之间就没法公平比较了（C-15）。

所以本模块**只接收补丁字符串**，不接收 Agent 的工作区路径 —— 从签名上就没法把
那份脏目录传进来。

## 为什么第 3 步不能省

生成补丁时（E3-T3 的 `normalize_patch()`）其实已经按路径过滤掉测试文件了。这里再
强制还原一遍，是**第二道独立的防线**（C-16）：两处实现只要有一处写出 bug，
基准都不会被攻破。

还原分两半，缺一不可：

- **已跟踪的文件** → `git checkout -- <具体路径>`。AI 改了或删了 `tests/test_a.py`，
  这一步把它还原成 base 的样子。
- **AI 新增的文件** → 逐个删（C-63）。`git checkout` 只管已跟踪的文件，
  **删不掉新建的未跟踪文件** —— AI 完全可以新建一个 `conftest.py` 做猴子补丁。

**绝不对整个工作区 `git clean -fd`**（C-63a 明令禁止）：那会把 AI 合法新增的源文件
一起删掉。修 bug 时新建一个模块文件是完全正常的行为，删掉它等于把正确答案删了。
这里先算出"归一化之后确认命中受保护规则"的具体文件清单，再逐个删（C-63b）。

扫新增文件时用 `include_ignored=True`：基线忽略清单里有 `__pycache__/`，
不带这个参数就列不出 `tests/__pycache__/conftest.cpython-311.pyc`，也就删不掉。

## 受保护清单必须带上该题的 test_patch_paths

用 `enforcement_patterns(task.test_patch_paths)` 生成，**不是**
`agent_visible_patterns()`（那份是下发给 AI 的，故意不含题目信息）。

理由：有些题目的测试改动会带上名字完全不像测试的 fixture 文件
（`tests/fixtures/reconnect.json` 这种），靠通配符匹配不到。漏掉它们，
AI 改了就生效，**解决率会静悄悄地偏高**。

## 只跑 F2P + P2P，不跑全量

C-17 的建议。好处是快（这对 6 小时的目标很关键），代价是发现不了这两个集合之外的
问题。这是有意的取舍：P2P 这个集合本身就是我们定义的"回归检查范围"，
题目验证阶段（E1-T3）才跑全量。

## 镜像从哪来

`image` 是必填参数，本模块**不关心镜像哪来的**。现在 Golden 题用
`make images` 建的 `bench-golden:py311`；E2-T3 的镜像分层构建器到位之后，
换个镜像名就行，这里一行都不用改。

`install_command` 不在这里跑 —— 按 ADR-008 它属于建镜像的时候，
而且测试阶段断网（C-31），容器里也装不了。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.enums import InfraOutcome
from app.domain.execution_plan import ExecutionPlan
from app.domain.protected_paths import enforcement_patterns, is_protected, normalize_path
from app.infrastructure.logging import get_logger
from app.judge.report_parser import ParsedReport, parse_pytest_report
from app.sandbox.container import (
    WORKSPACE_TARGET,
    BindMount,
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    ResourceLimits,
    Stage,
    build_env,
    classify_outcome,
)
from app.sandbox.container import run_in_container as _run_in_container
from app.sandbox.git_cli import GitError, run_git
from app.sandbox.workspace import Workspace, materialize_workspace

logger = get_logger(__name__)

#: Golden 题的测试镜像，和 Makefile 里的 `GOLDEN_IMAGE` 对齐。
#: 只是个方便的默认值，真实评测由 `environment_specs` 决定用哪个镜像。
DEFAULT_GOLDEN_IMAGE = "bench-golden:py311"


class ExecutionError(RuntimeError):
    """执行器自己出的错（不是被测 AI 的问题）。带上该记哪个 `infra_outcome`。"""

    def __init__(self, message: str, outcome: InfraOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class ProtectedPathRestore:
    """第 3 步干了什么 —— 防作弊第二道防线的证据。

    这份记录有两个下游用途，都不能少：

    - `attempted` 就是协议 C-08b / C-13c 里的 `protected_path_edit_attempted`。
      C-13d 要求它为 True 时**即使最终没出现 MISSING 也要触发人工复核**。
    - `restored` / `deleted` 是"我们确实挡住了"的可复核证据。只说"过滤了"
      而拿不出被丢弃的清单，防作弊就成了一句口号。
    """

    #: 被还原的已跟踪文件（AI 改过或删过）。
    restored: tuple[str, ...] = ()
    #: 被删掉的、AI 新增的受保护文件（C-63）。
    deleted: tuple[str, ...] = ()

    @property
    def attempted(self) -> bool:
        """AI 到底有没有伸手碰受保护路径。"""
        return bool(self.restored or self.deleted)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """跑完一轮测试之后我们知道的全部事实。

    和 `ContainerResult` 一样，这里只有**事实**，没有"修好了没有"的结论 ——
    那是 E4-T3 看 `report` 之后的事。
    """

    #: 平台这一轮有没有出故障。`SUCCESS` 不代表 AI 修好了，只代表我们跑完了。
    infra_outcome: InfraOutcome
    #: 解析出来的测试报告。前几步就失败时为 None。
    report: ParsedReport | None
    #: 第 3 步的证据。
    restore: ProtectedPathRestore
    #: 测试容器的原始结果。前几步就失败时为 None。
    container: ContainerResult | None
    #: `pre_test_command` 的容器结果，没有该命令时为 None。
    pre_test: ContainerResult | None = None
    #: 出错时的人话说明。
    problem: str | None = None
    #: 实际跑了哪些用例 ID（F2P + P2P，去重保序）。
    requested_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.infra_outcome is InfraOutcome.SUCCESS and self.report is not None


def _apply_patch(workspace: Workspace, patch: str, *, label: str, outcome: InfraOutcome) -> None:
    """把一段 diff 打进工作区。打不上就抛 `ExecutionError`。

    用 `git apply` 而不是 `patch`：前者认得 git 的重命名、模式变更和二进制段。
    `--3way` 让上下文对不齐时还能靠 blob 内容合，这是 §11.4 对标准化补丁的要求。

    空补丁直接返回 —— 空补丁是合法输入（Noop 哨兵交的就是空的），
    交给 `git apply` 会以 "unrecognized input" 收场，那是个误导人的错误。
    """
    if not patch.strip():
        return
    patch_file = workspace.path / f".bench-{label}.patch"
    # 结尾没有换行的话 git apply 会报 "corrupt patch at line N"，而补丁本身是好的
    patch_file.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
    try:
        run_git(
            ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
            cwd=workspace.path,
        )
    except GitError as exc:
        raise ExecutionError(f"{label} 打不上：{exc}", outcome) from exc
    finally:
        patch_file.unlink(missing_ok=True)

    # 把索引拨回 HEAD，工作区内容不动。
    #
    # **这一步不是收尾工作，是下一步的前提。** `git apply --3way` 走三方合并那条路时
    # 会顺手把结果**暂存进索引**，于是 AI 新建的 `conftest.py` 变成"已暂存的新增文件"。
    # 那样一来 `git diff --name-only HEAD` 会列出它，而它在 HEAD 里根本不存在，
    # 强制还原那一步的 `git checkout HEAD -- conftest.py` 就会报
    # "pathspec did not match any file(s) known to git" —— **防作弊直接崩在这儿**
    # （2026-09-05 被 test_added_conftest_does_not_help 抓到）。
    #
    # reset 之后状态是统一的：改过的跟踪文件 = 未暂存改动，新建的文件 = 未跟踪文件。
    # 这两种正好对应还原那一步的两半。重命名也一样收敛成"旧路径被删 + 新路径未跟踪"。
    run_git(["reset", "--quiet", "HEAD"], cwd=workspace.path)


def _changed_tracked_paths(workspace: Workspace) -> list[str]:
    """已跟踪文件里，相对 base 提交有改动的（含被删掉的）。"""
    raw = run_git(["diff", "--name-only", "-z", "HEAD"], cwd=workspace.path).stdout
    return [normalize_path(p) for p in raw.split("\0") if p]


def restore_protected_paths(
    workspace: Workspace, patterns: tuple[str, ...]
) -> ProtectedPathRestore:
    """第 3 步：把 AI 对受保护路径的手脚全部抹掉（C-16、C-63）。

    两半分开做，因为 git 的两条命令各管一半：

    1. `git checkout -- <路径>` 还原**已跟踪**的文件（被改的、被删的）。
    2. 逐个 `unlink` 删掉 AI **新增**的受保护文件 —— `checkout` 对未跟踪文件无效。

    第 2 半只删"归一化之后确认命中受保护规则"的具体文件（C-63b），
    **绝不** `git clean -fd`（C-63a）—— 那会连 AI 合法新增的源文件一起删掉。
    """
    restored = sorted(p for p in _changed_tracked_paths(workspace) if is_protected(p, patterns))
    if restored:
        # 一次 checkout 多个路径。`--` 是必须的：路径恰好和某个分支重名时，
        # 没有它 git 会把它当分支名，报 "pathspec did not match"
        run_git(["checkout", "HEAD", "--", *restored], cwd=workspace.path)

    deleted = []
    # include_ignored=True 是关键：基线忽略清单里有 __pycache__/，
    # 不带它就列不出 tests/__pycache__/*.pyc，也就删不掉
    for candidate in workspace.untracked_files(include_ignored=True):
        path = normalize_path(candidate)
        if not is_protected(path, patterns):
            continue
        target = workspace.path / path
        if target.is_file() or target.is_symlink():
            target.unlink()
            deleted.append(path)

    if restored or deleted:
        logger.warning(
            "agent 碰了受保护路径，已强制还原",
            restored=restored,
            deleted=sorted(deleted),
        )
    return ProtectedPathRestore(restored=tuple(restored), deleted=tuple(sorted(deleted)))


def _limits_of(plan: ExecutionPlan) -> ResourceLimits:
    """题目的资源预算翻译成容器限额。

    sandbox 层不能依赖 benchmark（import-linter 契约），所以这个映射只能由上层做。
    """
    return ResourceLimits(
        cpus=plan.sandbox_cpu,
        memory_mb=plan.sandbox_memory_mb,
        pids_limit=plan.sandbox_pids_limit,
    )


def _spec(
    plan: ExecutionPlan,
    workspace: Workspace,
    command: Sequence[str],
    *,
    image: str,
    run_id: str | None,
) -> ContainerSpec:
    """测试阶段的容器规格。

    `network=NONE` 是协议 C-31 的硬性要求：`AGENT_RUNNING` 是唯一允许联网的阶段。
    断网还顺带挡住了一类作弊 —— 测试跑起来之后再去网上抓答案。

    工作区**可写**：pytest 要往 `report/junit.xml` 写报告，那是我们取报告的唯一通道
    （容器退出后从宿主机这一侧读挂载目录）。
    """
    return ContainerSpec(
        image=image,
        command=list(command),
        timeout_s=plan.test_timeout_s,
        stage=Stage.TEST,
        limits=_limits_of(plan),
        network=NetworkMode.NONE,
        mounts=(BindMount.workspace(workspace.path),),
        workdir=WORKSPACE_TARGET,
        env=build_env(),
        run_id=run_id,
    )


def execute_tests(
    plan: ExecutionPlan,
    agent_patch: str,
    *,
    mirror_path: Path,
    workspace_dir: Path,
    image: str = DEFAULT_GOLDEN_IMAGE,
    run_id: str | None = None,
    client: Any = None,
    run_container: Callable[[ContainerSpec], ContainerResult] | None = None,
) -> ExecutionOutcome:
    """跑协议 C-14 的第 1–6 步，返回判定所需的全部事实。

    `agent_patch` 传**标准化之后**的补丁（E3-T3 的 `NormalizedPatch.patch`）。
    传原始补丁也不会出错 —— 第 3 步会把它对测试文件的改动抹掉，这正是
    第二道防线要证明的事 —— 但正式评测应该传标准化的那份，两道防线都要在。

    `workspace_dir` 必须不存在或为空目录。**不要**把 Agent 用过的工作区传进来，
    那违反 C-15；本函数会自己从 `mirror_path` 重新导出一份。

    `client` 是 docker 客户端，原样透传给 `run_in_container`。
    `run_container` 是给测试用的接缝：传一个假的进来，就能在**不起容器**的前提下
    验证第 1–4 步和故障映射。真实评测两个都不用传。
    """
    ids = plan.test_ids
    patterns = enforcement_patterns(plan.test_patch_paths, plan.extra_protected_paths)
    restore = ProtectedPathRestore()

    try:
        # 1. 干净工作区（C-15）。物化失败是平台的问题 → WORKSPACE_ERROR
        try:
            workspace = materialize_workspace(
                mirror_path=mirror_path, base_commit=plan.base_commit, dest=workspace_dir
            )
        except Exception as exc:  # WorkspaceError 及其底下的 GitError
            raise ExecutionError(f"工作区物化失败：{exc}", InfraOutcome.WORKSPACE_ERROR) from exc

        # 2. 打 AI 的补丁
        _apply_patch(workspace, agent_patch, label="agent", outcome=InfraOutcome.PATCH_APPLY_FAILED)

        # 3. 强制还原受保护路径（C-16、C-63）—— 顺序不能和第 4 步换：
        #    先还原再打测试补丁，官方测试才是最后落地的那一份
        restore = restore_protected_paths(workspace, patterns)

        # 4. 打官方测试补丁。打不上说明题目坏了，不是 AI 的锅
        _apply_patch(
            workspace, plan.test_patch, label="test", outcome=InfraOutcome.TEST_DISCOVERY_ERROR
        )
    except ExecutionError as exc:
        return ExecutionOutcome(
            infra_outcome=exc.outcome,
            report=None,
            restore=restore,
            container=None,
            problem=str(exc),
            requested_ids=ids,
        )

    run = run_container or (lambda spec: _run_in_container(spec, client=client))

    # 4.5 pre_test_command（可选）。单独起一个容器：它的副作用只有落在工作区里的
    #     才留得下来（比如就地编译扩展），装到镜像 site-packages 里的留不下 ——
    #     那种事按 ADR-008 属于建镜像阶段。
    pre_test = None
    if plan.pre_test_command:
        pre_test = run(
            _spec(
                plan,
                workspace,
                shlex.split(plan.pre_test_command),
                image=image,
                run_id=run_id,
            )
        )
        if not pre_test.ok:
            # pre_test 非零退出是真故障（不像测试阶段，那里非零是正常的），
            # classify_outcome 不翻译退出码，所以这里补一个 ENV_BUILD_FAILED
            outcome = classify_outcome(pre_test, stage=Stage.TEST)
            if outcome is InfraOutcome.SUCCESS:
                outcome = InfraOutcome.ENV_BUILD_FAILED
            return ExecutionOutcome(
                infra_outcome=outcome,
                report=None,
                restore=restore,
                container=None,
                pre_test=pre_test,
                problem=f"pre_test_command 失败（退出码 {pre_test.exit_code}）",
                requested_ids=ids,
            )

    # 5. 跑测试。只跑 F2P + P2P 列的用例（C-17）
    container = run(
        _spec(plan, workspace, [*shlex.split(plan.test_command), *ids], image=image, run_id=run_id)
    )

    # 非零退出码在测试阶段是**正常的**（有用例失败就非零），不能当故障。
    # 只有 OOM、超时、容器起不来才是平台的问题 —— 这正是 classify_outcome 的分工。
    outcome = classify_outcome(container, stage=Stage.TEST)
    if outcome is not InfraOutcome.SUCCESS:
        return ExecutionOutcome(
            infra_outcome=outcome,
            report=None,
            restore=restore,
            container=container,
            pre_test=pre_test,
            problem=f"测试容器异常结束：{outcome.value}",
            requested_ids=ids,
        )

    # 6. 解析报告。报告在工作区里（容器写、宿主机读，靠的是同一个绑定挂载）
    report = parse_pytest_report(
        workspace.path / plan.test_report_path,
        container.stdout,
        container.stderr,
        repo_root=WORKSPACE_TARGET,
    )
    return ExecutionOutcome(
        infra_outcome=InfraOutcome.SUCCESS,
        report=report,
        restore=restore,
        container=container,
        pre_test=pre_test,
        requested_ids=ids,
    )


__all__ = [
    "DEFAULT_GOLDEN_IMAGE",
    "ExecutionError",
    "ExecutionOutcome",
    "ProtectedPathRestore",
    "execute_tests",
    "restore_protected_paths",
]
