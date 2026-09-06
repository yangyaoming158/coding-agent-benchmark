"""端到端评测单元（E4-T4，M1 里程碑）。

一句话：**把一道题从"还没开始"跑到"有结论"，中间每一步的证据都留下来。**

    PREPARING ──▶ AGENT_RUNNING ──▶ PATCH_CAPTURED ──▶ TESTING ──▶ JUDGING ──▶ COMPLETED
       物化工作区     跑被测 AI          抓+归一化补丁      跑测试      判定         / FAILED

前面四个 Epic 各做了一段，这里负责把它们接起来，并且**接口不留猜测**：

| 步骤 | 谁干的 | 在哪 |
|:---|:---|:---|
| 物化干净工作区 | E2-T1 | `app.sandbox.workspace` |
| 跑被测 AI | E3-T1/T2 | `app.runner.adapters` |
| 抓补丁 + 归一化 | E3-T3 | `app.runner.patch` |
| 打补丁 + 还原 + 跑测试 | E4-T2 | `app.evaluation.executor` |
| 解析报告 | E4-T1 | `app.judge.report_parser` |
| 判定 | E4-T3 | `app.judge.decision` |

## 两份工作区，不是一份

Agent 用一份，测试用**另一份新物化的**（协议 C-15）。被测 AI 在自己那份里可能装了包、
改了配置、留了临时文件；拿它跑测试，测出来的就不只是"补丁对不对"了，
不同 AI 之间也没法公平比较。

代价是多物化一次（Golden 题几十毫秒，真实仓库几秒）。这个代价必须付。

## agent_started_at 的置位时刻是有定义的

协议 C-77 定死了：**Agent 容器成功启动、且任务输入已写入其标准输入的那一刻**。

定这么死是因为整张合法组合表都靠这个字段区分"没给 AI 机会"（`NOT_ATTEMPTED`）和
"给了机会但我们没拿到结论"（`NULL`）。这里的做法是：`runner.run()` 一旦返回结果，
就用它自报的 `started_at`；`run()` 抛异常则看异常类型 —— probe 阶段的失败算未启动。

## 异常怎么映射

每一步的异常都要落到一个 `infra_outcome` 上，**禁止让异常直接冒出去**。
冒出去的后果是这条记录停在非终态，永远不会被判定，也不会进任何统计 ——
它只是消失了，而且没人会发现。

    物化失败        → WORKSPACE_ERROR
    适配器自己崩了   → AGENT_RUNTIME_ERROR
    适配器超时      → AGENT_TIMEOUT（C-09a：仍然保存已改出来的补丁，但不跑测试）
    补丁打不上      → PATCH_APPLY_FAILED
    测试补丁打不上   → TEST_DISCOVERY_ERROR
    其余没预料到的   → HARNESS_ERROR

## 制品该落盘的都要落盘

判定结论只有配上证据才有意义。这里落六样：原始补丁、标准化补丁、
Agent 的 stdout/stderr、测试容器的 stdout/stderr、测试报告原文。

**原始补丁和标准化补丁两份都要留**。只留后者的话，"AI 试图改测试文件"这个行为
就再也查不到了，而它是防作弊分析的主要证据（协议 C-08b）。

## 这里不做的事

- **不写数据库**。落库在 `app.evaluation.persistence`，分开是为了能不带数据库
  测编排、不带 Docker 测落库。
- **不重试**。重试是新建一条记录（`attempt_no` 加 1），由 E5 的队列层负责；
  协议 C-32 **禁止**把已有记录的状态改回去。
- **不跑 C-20 的对照组**。`TEST_TIMEOUT` 时判定引擎会抛
  `ControlRunRequiredError`，这里如实记成需要对照组，交给 E5。
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import ArtifactKind, InfraOutcome, LifecycleStatus, PatchKind
from app.domain.execution_plan import ExecutionPlan
from app.domain.protected_paths import enforcement_patterns
from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE, ExecutionOutcome, execute_tests
from app.infrastructure.logging import get_logger
from app.judge.decision import AgentFacts, ControlRunRequiredError, Verdict, judge
from app.runner.patch import NormalizedPatch, capture_agent_patch, normalize_patch
from app.runner.protocol import (
    AGENT_STDERR_FILENAME,
    AGENT_STDOUT_FILENAME,
    AGENT_TRAJECTORY_FILENAME,
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    OOM_KILLED,
    RUNTIME_ERROR,
    AgentRunner,
    AgentRunResult,
    AgentTaskInput,
    ProtocolError,
)
from app.sandbox.container import SandboxError
from app.sandbox.workspace import Workspace, WorkspaceError, materialize_workspace
from app.storage.base import ArtifactRef, ArtifactStore

logger = get_logger(__name__)

#: 落盘的制品和它们的 content-type。文本一律 gzip 压缩后存（日志压缩比常在 10:1）。
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
_PATCH_CONTENT_TYPE = "text/x-diff; charset=utf-8"
_XML_CONTENT_TYPE = "application/xml"
#: 轨迹是一行一个 JSON 事件（§9.5），不是一整份 JSON 文档，所以不能写 application/json
_JSONL_CONTENT_TYPE = "application/x-ndjson"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TaskRunInputs:
    """跑一次评测要的全部输入。

    刻意分成 `plan` 和 `agent_input` 两半：前者含官方答案那一侧的东西
    （`test_patch`、F2P 名单），后者是**下发给被测 AI 的全部内容**。
    分开之后，"什么能给 AI 看"这件事在类型上就是明确的，不用每次读代码去确认。
    """

    plan: ExecutionPlan
    agent_input: AgentTaskInput
    #: 本地 git 镜像仓库，两次物化都从它导出。
    mirror_path: Path
    #: 工作区落在哪个目录下。函数会在里面建 `agent/` 和 `test/` 两个子目录。
    scratch_dir: Path
    #: 测试容器用的镜像。E2-T3 到位后由环境规格提供。
    image: str = DEFAULT_GOLDEN_IMAGE
    #: Agent 的墙钟预算（秒）。
    agent_timeout_s: int = 720
    #: 这次评测的 id，写进容器标签和制品 key，方便事后对回具体哪一次运行。
    run_key: str = "adhoc"


@dataclass(frozen=True, slots=True)
class Timings:
    """各阶段的时刻。字段名和 `evaluation_task_runs` 的列一一对应。"""

    prepare_started_at: datetime | None = None
    #: 置位时刻见协议 C-77。为 None 表示 AI 从未启动 —— 这正是 `NOT_ATTEMPTED` 的判据（C-69）。
    agent_started_at: datetime | None = None
    agent_finished_at: datetime | None = None
    test_started_at: datetime | None = None
    test_finished_at: datetime | None = None
    judged_at: datetime | None = None
    completed_at: datetime | None = None

    @staticmethod
    def _ms(start: datetime | None, end: datetime | None) -> int | None:
        if start is None or end is None:
            return None
        return int((end - start).total_seconds() * 1000)

    @property
    def agent_duration_ms(self) -> int | None:
        return self._ms(self.agent_started_at, self.agent_finished_at)

    @property
    def test_duration_ms(self) -> int | None:
        return self._ms(self.test_started_at, self.test_finished_at)

    @property
    def total_duration_ms(self) -> int | None:
        return self._ms(self.prepare_started_at, self.completed_at)


@dataclass(frozen=True, slots=True)
class TaskRunOutcome:
    """一次评测跑完之后我们知道的全部东西。

    `verdict` 里已经有了三字段结论（而且构造时过了 C-78 的合法组合校验），
    其余字段是证据和统计，落库时逐个写进 `evaluation_task_runs` 的列。
    """

    verdict: Verdict
    timings: Timings
    #: 标准化补丁及其统计。AI 没跑起来时为 None。
    patch: NormalizedPatch | None = None
    #: 适配器自报的结果。没跑起来或者崩了时为 None。
    agent_result: AgentRunResult | None = None
    #: 测试执行的原始结果。没跑到那一步时为 None。
    execution: ExecutionOutcome | None = None
    #: 落盘的日志类制品：种类 → 引用。写进 `artifacts` 表。
    artifacts: Mapping[ArtifactKind, ArtifactRef] = field(default_factory=dict)
    #: 落盘的补丁：种类 → 引用。补丁另有一张 `patch_artifacts` 表，
    #: 因为它还要带 files_changed / lines_added 这些统计，和通用制品不是一回事。
    patches: Mapping[PatchKind, ArtifactRef] = field(default_factory=dict)
    #: 出错时的机器可读代码（用 `InfraOutcome` 的值）和人话摘要。
    error_code: str | None = None
    error_message_excerpt: str | None = None
    #: `TEST_TIMEOUT` 时为 True：判定被卡住了，要 E5 按 C-20 跑对照组再判。
    needs_control_run: bool = False

    @property
    def lifecycle_status(self) -> LifecycleStatus:
        return self.verdict.lifecycle_status

    @property
    def infra_outcome(self) -> InfraOutcome:
        return self.verdict.infra_outcome

    @property
    def resolved(self) -> bool:
        return self.verdict.resolved


class _AbortError(Exception):
    """内部用：某一步失败了，带着该记的 `infra_outcome` 直接跳到收尾。"""

    def __init__(self, outcome: InfraOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


def _store_text(
    store: ArtifactStore | None,
    artifacts: dict[ArtifactKind, ArtifactRef],
    kind: ArtifactKind,
    key: str,
    text: str,
    *,
    content_type: str = _TEXT_CONTENT_TYPE,
) -> None:
    """存一份文本制品。空文本不存 —— 一堆 0 字节的制品只会淹没真正有内容的那些。

    存制品失败**不能让整次评测失败**：判定结论已经算出来了，为了一个日志文件把
    它丢掉是本末倒置。这里只记一条警告。
    """
    if store is None or not text:
        return
    try:
        artifacts[kind] = store.put(key, text.encode("utf-8"), content_type=content_type)
    except Exception as exc:  # 存制品失败不该影响判定结论
        logger.warning("制品落盘失败", kind=kind.value, key=key, error=str(exc))


def _store_patch(
    store: ArtifactStore | None,
    patches: dict[PatchKind, ArtifactRef],
    kind: PatchKind,
    key: str,
    text: str,
) -> None:
    """存一份补丁。**空补丁也要存**，和日志不一样。

    空的标准化补丁是一条有意义的记录（`EMPTY_PATCH` 的证据），而且
    `patch_artifacts` 表有 `is_empty` 列专门记它。不存的话，"AI 交了空补丁"和
    "我们没来得及存"就分不出来了。
    """
    if store is None:
        return
    try:
        patches[kind] = store.put(key, text.encode("utf-8"), content_type=_PATCH_CONTENT_TYPE)
    except Exception as exc:  # 存制品失败不该影响判定结论
        logger.warning("补丁落盘失败", kind=kind.value, key=key, error=str(exc))


def execute_task_run(
    runner: AgentRunner,
    inputs: TaskRunInputs,
    *,
    store: ArtifactStore | None = None,
    agent_config: Any = None,
    client: Any = None,
) -> TaskRunOutcome:
    """跑完一道题：物化 → 跑 AI → 抓补丁 → 跑测试 → 判定。

    **不抛异常**（除了调用方传错参数这种编程错误）。每一步的失败都被映射成一个
    `infra_outcome` 记进结果里 —— 让异常冒出去的话，这条记录会停在非终态，
    永远不会被判定，也不会进任何统计，等于凭空消失了。

    `agent_config` 原样透传给 `runner.run()`，不传就用 `AgentConfig()` 的默认值。
    `store` 为 None 时不落制品（单测用），真实评测必须给。
    """
    from app.runner.protocol import AgentConfig  # 局部导入：默认值要一个实例

    config = agent_config if agent_config is not None else AgentConfig()
    # 制品往哪写由 harness 说了算，调用方给的值一律覆盖 —— 制品的位置和
    # `run_key` 是绑定的，让调用方决定的话，同一次运行的东西会散到几个地方去
    config = replace(config, artifact_dir=inputs.scratch_dir / "agent-io")
    plan = inputs.plan
    artifacts: dict[ArtifactKind, ArtifactRef] = {}
    patches: dict[PatchKind, ArtifactRef] = {}
    timings = Timings(prepare_started_at=_now())
    patch: NormalizedPatch | None = None
    agent_result: AgentRunResult | None = None

    try:
        # ── PREPARING：给 Agent 物化一份工作区 ──
        try:
            agent_ws = materialize_workspace(
                mirror_path=inputs.mirror_path,
                base_commit=plan.base_commit,
                dest=inputs.scratch_dir / "agent",
            )
        except (WorkspaceError, OSError) as exc:
            raise _AbortError(InfraOutcome.WORKSPACE_ERROR, f"工作区物化失败：{exc}") from exc

        # ── AGENT_RUNNING ──
        try:
            agent_result = runner.run(inputs.agent_input, agent_ws, config)
        except SandboxError as exc:
            # docker 连不上、镜像不在、容器起不来 —— 平台的锅，不是 AI 的。
            # 这一条要排在下面那个 except Exception 前面：落到那里会被记成
            # AGENT_RUNTIME_ERROR，也就是记在被测 AI 头上，白白拉低它的解决率。
            # E3-T4 之前没有适配器会起容器，所以这条路径以前不存在
            timings = replace(timings, agent_started_at=_now(), agent_finished_at=_now())
            raise _AbortError(InfraOutcome.SANDBOX_ERROR, f"Agent 容器起不来：{exc}") from exc
        except ProtocolError as exc:
            # 适配器输出的 JSON 读不出来。它确实跑起来了，只是没好好说话
            timings = replace(timings, agent_started_at=_now(), agent_finished_at=_now())
            raise _AbortError(
                InfraOutcome.AGENT_RUNTIME_ERROR, f"适配器输出不符合协议：{exc}"
            ) from exc
        except Exception as exc:  # 适配器什么都可能抛
            timings = replace(timings, agent_started_at=_now(), agent_finished_at=_now())
            raise _AbortError(
                InfraOutcome.AGENT_RUNTIME_ERROR, f"适配器崩了：{type(exc).__name__}: {exc}"
            ) from exc

        timings = replace(
            timings,
            agent_started_at=agent_result.started_at,
            agent_finished_at=agent_result.finished_at,
        )
        _store_text(
            store,
            artifacts,
            ArtifactKind.AGENT_STDOUT,
            f"{inputs.run_key}/agent.log",
            _agent_log(agent_result) + _read_side_file(config, AGENT_STDOUT_FILENAME),
        )
        _store_text(
            store,
            artifacts,
            ArtifactKind.AGENT_STDERR,
            f"{inputs.run_key}/agent.err.log",
            _read_side_file(config, AGENT_STDERR_FILENAME),
        )
        _store_text(
            store,
            artifacts,
            ArtifactKind.TRAJECTORY,
            f"{inputs.run_key}/trajectory.jsonl",
            _read_side_file(config, AGENT_TRAJECTORY_FILENAME),
            content_type=_JSONL_CONTENT_TYPE,
        )

        # ── PATCH_CAPTURED ──
        patterns = enforcement_patterns(plan.test_patch_paths, plan.extra_protected_paths)
        try:
            patch = _take_patch(agent_result, agent_ws, patterns)
        except Exception as exc:  # git 出问题算平台的锅
            raise _AbortError(InfraOutcome.HARNESS_ERROR, f"抓补丁失败：{exc}") from exc

        # 两份都存：只留标准化的，"AI 试图改测试文件"就再也查不到了（C-08b）
        _store_patch(
            store,
            patches,
            PatchKind.AGENT_RAW,
            f"{inputs.run_key}/patch-raw.diff",
            agent_result.patch or "",
        )
        _store_patch(
            store, patches, PatchKind.AGENT_NORMALIZED, f"{inputs.run_key}/patch.diff", patch.text
        )

        # 适配器自己报了错误 → 责任在 AI。C-09a：超时也要保存补丁，但不跑测试
        if agent_result.error is not None:
            raise _AbortError(
                _outcome_for_agent_error(agent_result),
                f"适配器报错：{agent_result.error.code}",
            )

        # ── TESTING ──
        timings = replace(timings, test_started_at=_now())
        execution = execute_tests(
            plan,
            patch.text,
            mirror_path=inputs.mirror_path,
            workspace_dir=inputs.scratch_dir / "test",
            image=inputs.image,
            run_id=inputs.run_key,
            client=client,
        )
        timings = replace(timings, test_finished_at=_now())

        if execution.container is not None:
            _store_text(
                store,
                artifacts,
                ArtifactKind.TEST_STDOUT,
                f"{inputs.run_key}/test.log",
                _container_log(execution),
            )
        if execution.report is not None:
            _store_text(
                store,
                artifacts,
                ArtifactKind.TEST_REPORT_XML,
                f"{inputs.run_key}/report.xml",
                _report_text(execution),
                content_type=_XML_CONTENT_TYPE,
            )

        # ── JUDGING ──
        facts = AgentFacts(
            agent_started=True,
            exited_normally=agent_result.error is None,
            normalized_patch_empty=patch.is_empty,
            raw_patch_empty=patch.raw_patch_empty,
            protected_path_edit_attempted=(
                patch.protected_path_edit_attempted or execution.restore.attempted
            ),
        )
        try:
            verdict = judge(
                infra_outcome=execution.infra_outcome,
                report=execution.report,
                fail_to_pass=plan.fail_to_pass,
                pass_to_pass=plan.pass_to_pass,
                facts=facts,
            )
        except ControlRunRequiredError as exc:
            # C-20：测试超时要先跑不打补丁的对照组才能判，那是 E5 的活
            timings = replace(timings, judged_at=_now(), completed_at=_now())
            return TaskRunOutcome(
                verdict=_fallback_verdict(InfraOutcome.TEST_TIMEOUT, agent_started=True),
                timings=timings,
                patch=patch,
                agent_result=agent_result,
                execution=execution,
                artifacts=artifacts,
                patches=patches,
                error_code=InfraOutcome.TEST_TIMEOUT.value,
                error_message_excerpt=str(exc)[:2000],
                needs_control_run=True,
            )

        timings = replace(timings, judged_at=_now(), completed_at=_now())
        return TaskRunOutcome(
            verdict=verdict,
            timings=timings,
            patch=patch,
            agent_result=agent_result,
            execution=execution,
            artifacts=artifacts,
            patches=patches,
            error_code=None if execution.ok else execution.infra_outcome.value,
            error_message_excerpt=execution.problem,
        )

    except _AbortError as abort:
        return _abort_outcome(abort, timings, patch, agent_result, artifacts, patches)
    except Exception as exc:  # 兜住一切，绝不让记录停在非终态
        logger.error("评测单元遇到未预料的异常", error=str(exc), tb=traceback.format_exc()[-2000:])
        return _abort_outcome(
            _AbortError(InfraOutcome.HARNESS_ERROR, f"未预料的异常：{type(exc).__name__}: {exc}"),
            timings,
            patch,
            agent_result,
            artifacts,
            patches,
        )


def _take_patch(
    result: AgentRunResult, workspace: Workspace, patterns: tuple[str, ...]
) -> NormalizedPatch:
    """拿到这次要判定的补丁。

    **规则：适配器自己报了非空补丁就用它，否则去工作区里 `git diff`。**

    为什么不看 `patch_source`：那个字段是**归因用的元数据**（"这段 diff 是跑
    git diff 得来的，还是 AI 自己打印的"—— 后者行号容易写错），
    **不是**"补丁从哪拿"的路由信号。Oracle 就是反例：它标的是 `git_diff`，
    却在文档里明写"workspace 一眼都不看"，直接把官方补丁字符串交出来。
    照 `patch_source` 分流的话，Oracle 会被当成改过工作区、于是抓到一个空 diff，
    **解决率 100% 的哨兵会变成 0%**（2026-09-05 冒烟时踩到）。

    反过来，适配器报空补丁但工作区里确实有改动时，以工作区为准 —— 工作区是
    "它到底干了什么"的事实来源。这种情况会记一条警告：适配器没如实报告自己的改动。
    """
    if result.has_patch:
        return normalize_patch(result.patch, protected_patterns=patterns)
    captured = capture_agent_patch(workspace, protected_patterns=patterns)
    if not captured.raw_patch_empty:
        logger.warning(
            "适配器报了空补丁，但工作区里有改动，以工作区为准",
            agent=result.agent_name,
            files_changed=captured.raw_stats.files_changed,
        )
    return captured


#: 适配器自报的错误码 → `infra_outcome`。**查表，不按子串猜。**
#:
#: 猜子串踩过一次：Mock 的超时报的是 `deadline_exceeded`，里面根本没有 "timeout"
#: 这个词，于是超时被错判成了运行时错误（2026-09-05）。两者按 C-18 都判
#: `UNRESOLVED`，但重试次数不同（超时 0 次、运行时错误 1 次），猜错就是白跑一遍。
_AGENT_ERROR_TO_INFRA: dict[str, InfraOutcome] = {
    DEADLINE_EXCEEDED: InfraOutcome.AGENT_TIMEOUT,
    AUTH_FAILED: InfraOutcome.AGENT_AUTH_ERROR,
    RUNTIME_ERROR: InfraOutcome.AGENT_RUNTIME_ERROR,
    OOM_KILLED: InfraOutcome.OOM_KILLED,
}


def _outcome_for_agent_error(result: AgentRunResult) -> InfraOutcome:
    """适配器自报的错误码翻译成 `infra_outcome`。

    认不出来的码一律当 `AGENT_RUNTIME_ERROR` —— 责任落在被测 AI 这一侧，
    不会冤枉平台，代价只是多重试一次。第三方适配器的错误码是自由字符串，
    我们自己写的适配器必须用 `app.runner.protocol` 里那几个规范值。
    """
    code = result.error.code if result.error else ""
    return _AGENT_ERROR_TO_INFRA.get(code, InfraOutcome.AGENT_RUNTIME_ERROR)


def _fallback_verdict(outcome: InfraOutcome, *, agent_started: bool) -> Verdict:
    """出错时也要产出一个合法的三字段组合。

    走 `judge()` 而不是自己拼，是为了让"哪个故障对应哪个结论"永远只有一处实现
    （协议 C-19），也顺带过一遍 C-78 的合法组合校验。
    """
    return judge(
        infra_outcome=outcome,
        report=None,
        fail_to_pass=(),
        facts=AgentFacts(agent_started=agent_started),
        control_run_timed_out=False if outcome is InfraOutcome.TEST_TIMEOUT else None,
    )


def _abort_outcome(
    abort: _AbortError,
    timings: Timings,
    patch: NormalizedPatch | None,
    agent_result: AgentRunResult | None,
    artifacts: dict[ArtifactKind, ArtifactRef],
    patches: dict[PatchKind, ArtifactRef],
) -> TaskRunOutcome:
    """把中途失败收成一个终态结果。

    `agent_started` 取自 `timings.agent_started_at` 是不是空的 —— 协议 C-69
    要求 `NOT_ATTEMPTED` 当且仅当它为空，这里就是那个"当且仅当"的落点。
    """
    started = timings.agent_started_at is not None
    verdict = _fallback_verdict(abort.outcome, agent_started=started)
    return TaskRunOutcome(
        verdict=verdict,
        timings=replace(timings, judged_at=_now(), completed_at=_now()),
        patch=patch,
        agent_result=agent_result,
        artifacts=artifacts,
        patches=patches,
        error_code=abort.outcome.value,
        error_message_excerpt=abort.message[:2000],
    )


def _read_side_file(config: Any, filename: str) -> str:
    """读适配器写在 `AgentConfig.artifact_dir` 里的一个文件，读不到就当空。

    读不到是常态，不是异常：哨兵适配器一个字节都不写。所以这里不报错、不记日志 ——
    每跑一次就刷三条"文件不存在"的警告，真正的问题会被淹掉。
    """
    directory: Path | None = getattr(config, "artifact_dir", None)
    if directory is None:
        return ""
    try:
        return (directory / filename).read_text(encoding="utf-8")
    except OSError:
        return ""


def _agent_log(result: AgentRunResult) -> str:
    """Agent 那一侧的可读摘要。完整轨迹由适配器自己写文件（协议 §9.5）。"""
    lines = [
        f"agent={result.agent_name} version={result.agent_version or '-'}",
        f"model={result.model or '-'} exit_code={result.exit_code}",
        f"duration_ms={result.duration_ms} turns={result.turns if result.turns else '-'}",
        f"patch_source={result.patch_source} patch_bytes={len(result.patch)}",
        f"stdout_bytes={result.raw_stdout_bytes} stderr_bytes={result.raw_stderr_bytes}",
    ]
    if result.error is not None:
        lines.append(f"error={result.error.code}: {result.error.message}")
    return "\n".join(lines) + "\n"


def _container_log(execution: ExecutionOutcome) -> str:
    container = execution.container
    if container is None:
        return ""
    parts = [f"exit_code={container.exit_code} duration_s={container.duration_s:.3f}"]
    if container.stdout:
        parts.append("--- stdout ---\n" + container.stdout)
    if container.stderr:
        parts.append("--- stderr ---\n" + container.stderr)
    return "\n".join(parts) + "\n"


def _report_text(execution: ExecutionOutcome) -> str:
    """测试报告的原文。

    拿的是解析之后的摘要而不是 XML 原文 —— 原文在测试工作区里，而工作区跑完就
    被清理了。够用：复核时要看的是"哪条用例什么状态"，那份信息一条不少。
    """
    report = execution.report
    if report is None:
        return ""
    lines = [f"<!-- source={report.source.value} truncated={report.truncated} -->"]
    for test_id, case in sorted(report.cases.items()):
        lines.append(f"{case.status.value}\t{test_id}")
    for error in report.collection_errors:
        lines.append(f"COLLECTION_ERROR\t{error.module_path}")
    return "\n".join(lines) + "\n"


def deadline_ms(timeout_s: int, *, now: float | None = None) -> int:
    """算 `AgentTaskInput.constraints.deadline_unix_ms`。

    用绝对时刻不用"还剩几秒"：适配器可能几秒后才真正开始干活，传相对值的话，
    这段启动时间就被白送给了 AI，两次运行的预算不一样。
    """
    return int(((time.time() if now is None else now) + timeout_s) * 1000)


__all__ = [
    "TaskRunInputs",
    "TaskRunOutcome",
    "Timings",
    "deadline_ms",
    "execute_task_run",
]
