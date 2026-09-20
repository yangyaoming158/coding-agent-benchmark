"""题目验证流水线（E1-T3，`docs/plan/03-benchmark-spec.md` §7.3 的八步）。

一句话：**在真容器里把一道题自己跑一遍，证明它当得起"题目"这两个字，并把证据留下。**

    S1 镜像仓库可用          失败 → INVALID(REPO_UNAVAILABLE)
    S2 物化到 base_commit    失败 → INVALID(COMMIT_MISSING)
    S3 环境镜像可用 + 取 digest  失败 → INVALID(ENV_UNBUILDABLE)
    S4 base + test_patch，跑全量套件，记下每条用例的基线状态   超时 → INVALID(TEST_TOO_SLOW)
    S5 每条 F2P 在基线里必须是 FAILED/ERROR    不满足 → INVALID(F2P_NOT_FAILING)
    S6 打上 gold_patch
    S7 F2P 必须全部通过                        不满足 → INVALID(GOLD_NOT_FIXING)
    S8 P2P 必须仍然通过 + 复跑查不稳定用例      不满足 → INVALID(GOLD_REGRESSION)
    ⇒ VALID（证据里写下镜像 digest、全量用例清单、耗时基线）

## 八步只起三次容器

S5 **不再逐条跑测试**。S4 明写"记录全量用例基线状态"，junit 报告里本来就有每条用例的
状态，S5 直接查这张表就够了。`cli/golden.py` 的六步验证是逐条起 pytest 的 ——
那是因为它只跑指定用例，一次跑完只能得出"至少挂了一条"，而这里要的"每一条都挂"
从全量报告里一眼就能读出来。§7.2(6) 说的 P2P 候选池也是从这份报告里来的。

于是真正起容器的只有：S4 一次（基线）、S7/S8 一次（打了 gold）、S8 复跑一次。

## 为什么复用 `execute_tests` 而不是自己跑测试

S4 = `execute_tests(plan, agent_patch="")` —— 空补丁，这就是 Noop 哨兵。
S7 = `execute_tests(plan, agent_patch=gold_patch)` —— 官方补丁，这就是 Oracle 哨兵。

不是图省事。验证要是走另一条跑测试的路，"这道题验过了"就不保证正式评测时判得对 ——
中间隔着容器规格、断网策略、补丁应用顺序、报告解析、用例 ID 归一化五道关，
任何一道两边不一致，结论都可能不同。协议 C-50 把 Oracle 100% / Noop 0% 定成题库
发布门槛，而这条流水线给出的正是每道题的那份证据。

**代价是本模块只能放在 `app.evaluation` 下。** import-linter 里
`app.evaluation | app.benchmark` 是并排的，并排就是互不可见，
放进 `app.benchmark` 就 import 不到执行器。`gold_patch` 是函数参数、不进
`ExecutionPlan`，所以"官方答案不进执行计划"那条边界不受影响。

## 三种失败，结论不一样

| 情况 | 结论 |
|:---|:---|
| 命中 §7.3 那七个 reason code 之一 | `INVALID` + code（题目本来就是 `VALID` 的话记 `QUARANTINED`）|
| 步骤失败但七个 code 都不对应（test_patch 打不上、报告不完整…）| `REVIEW_REQUIRED`，原文记进证据 |
| 平台自己出故障（OOM、容器起不来、连不上 docker）| **不下结论**，`state` 为 None |

第三行是这套东西的底线：平台自己坏了就说自己坏了，不能把账记到题目头上 ——
那和"把平台故障算进解决率"是同一类错误，只是换了个对象。

## 一次超时不等于隔离（C-20a）

首次验证时 S4 超时就是 `INVALID(TEST_TOO_SLOW)`，这是 §7.3 明写的。协议 C-20a 禁止的
是另一件事：**已经发布的题目**在正式评测时超时一次就被隔离 —— 那种情况要先按 C-20
跑对照组，只有题目复验也失败才隔离。两者不是一回事，别混。

`QUARANTINED` 在**这个模块**里只有一个来源：`previous_state` 已经是 `VALID` 的题目复验没过。
另一个来源在 `app.attribution.review_service`（E6-T3，2026-09-20）：人工盲检两人一致判 N2
（题目缺陷）也会隔离当前题。两条路都只改 `validation_state`，不动已发布快照。

## 不稳定用例：报出来，不偷偷改题

§7.2(7) 写的是"P2P 连跑 2 次不一致 → 该用例剔除"。剔除会改 `pass_to_pass`，
进而改 `content_hash`，而 `content_hash` 是数据集快照的身份证（§7.5）。
所以本模块**只报不改**：把该剔的用例列进证据，状态判 `REVIEW_REQUIRED`。
改不改由人或者数据集发布环节（E1-T6）决定。不稳定的 F2P 直接判
`GOLD_NOT_FIXING` —— 它不能稳定通过，就不算修好了。

## S3 现在只做 reuse 那一半

§7.3 的 S3 是"build/reuse env image"。镜像分层构建器是 E2-T3，还没做。
这里只查镜像在不在本地、取它的 digest；不在就判 `ENV_UNBUILDABLE`，
错误信息里明说要先 `make images`。**不会自动 build，也不会自动 pull**（ADR-008）。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.domain.enums import (
    InfraOutcome,
    TaskInvalidReason,
    TaskValidationState,
    TestStatus,
)
from app.domain.execution_plan import ExecutionPlan
from app.evaluation.executor import ExecutionOutcome, execute_tests
from app.infrastructure.logging import get_logger
from app.judge.report_parser import ParsedReport
from app.judge.test_ids import normalize_test_id
from app.sandbox.container import (
    WORKSPACE_TARGET,
    ContainerResult,
    ContainerSpec,
    ImageInfo,
    ImageNotFoundError,
    SandboxError,
    inspect_image,
)
from app.sandbox.git_cli import GitError
from app.sandbox.mirror import MirrorError, MirrorManager
from app.sandbox.workspace import WorkspaceError, materialize_workspace
from app.storage.base import ArtifactRef, ArtifactStore

logger = get_logger(__name__)

#: 流水线自身的版本。证据文档里要记 —— 三周后有人问"这道题当初是怎么验的"，
#: 得答得出来是哪一版规则验的。改了判定规则就要加这个号。
PIPELINE_VERSION = "1.0"

#: 证据文档的结构版本。字段增删时加这个号，读证据的代码据此分支。
#: 1.1（2026-09-15，E1-T7）：`task` 块加了 `suite_scope`，记这次验证跑的是全量还是声明的用例。
EVIDENCE_SCHEMA_VERSION = "1.1"

#: gold 侧默认跑几遍。至少 2 遍才谈得上"复跑查不稳定用例"（§7.3 S8）。
DEFAULT_REPEAT = 2

#: 基线耗时占到 `test_timeout_s` 这个比例就提请人工复核（§7.4 的"测试超时接近阈值"）。
#: 0.8 是个工程取值：留两成余量给机器负载波动，低于这个数正式评测时很容易踩线超时。
SLOW_TEST_RATIO = 0.8

#: "这条用例失败了"只认这两种状态。协议 C-12 禁止把 SKIPPED / XFAIL / MISSING
#: 当成通过，反过来也一样：它们同样不能算作"失败"——
#: 一条被 skip 掉的 F2P 证明不了 bug 存在。
FAILING_STATUSES: frozenset[TestStatus] = frozenset({TestStatus.FAILED, TestStatus.ERROR})

#: 这些 `infra_outcome` 是平台自己的故障，**不能**据此给题目下结论。
PLATFORM_FAULTS: frozenset[InfraOutcome] = frozenset(
    {
        InfraOutcome.OOM_KILLED,
        InfraOutcome.SANDBOX_ERROR,
        InfraOutcome.WORKSPACE_ERROR,
        InfraOutcome.HARNESS_ERROR,
        InfraOutcome.ENV_BUILD_FAILED,
    }
)

_JSON_CONTENT_TYPE = "application/json"
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
_XML_CONTENT_TYPE = "application/xml"

#: 制品 key 里的时间戳格式。**不能用 ISO 8601** —— `validate_key()` 只放行
#: `[A-Za-z0-9._/-]`，ISO 里的冒号会被当场拒收。
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _now() -> datetime:
    return datetime.now(UTC)


# ══════════════════════════════════════════════════════════════
# 输入 / 输出
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """验一道题要的全部输入。

    `gold_patch` 是**函数参数**而不是 `ExecutionPlan` 的字段：官方答案跟"跑一轮测试"
    这件事没关系，进了执行计划就多一条泄漏路径（见 `ExecutionPlan` 的模块文档）。
    验证阶段要用它，所以在这一层单独收。
    """

    plan: ExecutionPlan
    #: 官方修复补丁。S6 打上它，S7/S8 据此判断题目立不立得住。
    gold_patch: str
    #: `owner/repo`，用来定位 bare mirror。
    repo_name: str
    #: bare mirror 的存放根目录（`settings.mirror_root`）。
    mirror_root: Path
    #: 工作区和临时文件落在哪。函数会在里面建几个子目录，跑完由调用方清理。
    scratch_dir: Path
    #: 跑测试用的镜像。E2-T3 到位后由环境规格给。
    image: str
    #: 镜像不在本地时用它 clone。`golden://` 开头表示题目是生成的，没有上游可拉。
    repo_url: str | None = None
    #: 题目内容哈希，原样写进证据，方便事后确认"验的是哪一版题目"。
    content_hash: str | None = None
    #: `TaskDefinition.review_flags()` 的结果。八步全过但它非空 → `REVIEW_REQUIRED`。
    review_flags: tuple[str, ...] = ()
    #: 这道题现在是什么状态。已经是 `VALID` 的题复验失败要记 `QUARANTINED`（§7.4）。
    previous_state: TaskValidationState | None = None
    #: gold 侧跑几遍。小于 2 就跳过 S8 的不稳定用例检查。
    repeat: int = DEFAULT_REPEAT
    #: S4 / S7 / S8 跑全量套件还是只跑题目声明的用例（F2P ∪ P2P）。
    #:
    #: `full` 是 §7.3 的默认：P2P 候选池就从全量报告里来（§7.2(6)），挖掘题必须走它。
    #: `declared` 是给 **P2P 已经给定** 的题准备的 —— SWE-bench 官方题的 P2P 是官方定的
    #: （E1-T7，§8.6），不需要候选池；而 astropy / scikit-learn 的全量套件要跑几十分钟，
    #: 三轮下来一道题就是一小时。只跑声明的用例和正式评测跑的集合一样（C-17），
    #: 对"这道题判得对不对"这个问题没有损失；证据里会如实记下用的是哪一种。
    suite_scope: Literal["full", "declared"] = "full"

    @property
    def task_id(self) -> str:
        """题目 id。只有一份来源（`plan.task_id`），不再单独收一遍免得两边对不上。"""
        return self.plan.task_id


@dataclass(frozen=True, slots=True)
class StepResult:
    """一步的结论。`detail` 是给人看的一句话，会原样进证据文档。"""

    step: str
    name: str
    ok: bool
    detail: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """一次验证跑完之后我们知道的全部东西。

    `state` 为 None 表示**没得出结论**（平台自己出了故障）。这时调用方
    **不要**去改题目的 `validation_state` —— 把平台故障写成题目无效，
    下次就再也没人会去查真正的原因了。
    """

    state: TaskValidationState | None
    reason_code: TaskInvalidReason | None
    steps: tuple[StepResult, ...]
    #: 证据文档（`evidence.json` 的内容）。
    evidence: Mapping[str, Any]
    #: 落盘的制品：逻辑 key → 引用。
    artifacts: Mapping[str, ArtifactRef]
    #: `evidence.json` 的 uri，回填进 `benchmark_tasks.validation_evidence_uri`。
    evidence_uri: str | None
    #: 没得出结论时的人话说明。
    error: str | None
    validated_at: datetime
    #: 用到的镜像身份，S3 之前失败时为 None。
    image: ImageInfo | None = None

    @property
    def valid(self) -> bool:
        return self.state is TaskValidationState.VALID


class _Stop(Exception):  # noqa: N818 —— 这是控制流，不是错误，叫 Error 反而误导
    """跳出流水线，带上最终结论。三种用法互斥。"""

    def __init__(
        self,
        *,
        reason: TaskInvalidReason | None = None,
        review: str | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__(reason or review or error or "")
        self.reason = reason
        self.review = review
        self.error = error


# ══════════════════════════════════════════════════════════════
# 报告读数
# ══════════════════════════════════════════════════════════════


def status_of(report: ParsedReport, test_id: str) -> TestStatus:
    """按题目里写的 ID 从报告里取状态，取不到记 `MISSING`（协议 C-11）。

    走 `report.resolve()` 而不是直接下标：它还会试备选 ID 和路径后缀，
    那正是防假 `MISSING` 的地方（AGENTS.md §5.5 说的静默 bug 就出在这儿）。
    `repo_root` 必须和 `execute_tests` 解析报告时用的一致，否则容器里的
    `/workspace/tests/x.py` 和题目里的 `tests/x.py` 收敛不到一起。
    """
    case = report.resolve(test_id, repo_root=WORKSPACE_TARGET)
    return TestStatus.MISSING if case is None else case.status


def _statuses(report: ParsedReport, test_ids: Iterable[str]) -> dict[str, str]:
    """一批用例 ID → 状态，写进证据文档用。"""
    return {test_id: status_of(report, test_id).value for test_id in test_ids}


def _passing_cases(report: ParsedReport) -> set[str]:
    """报告里通过的用例（归一化 ID）。P2P 候选池就是从这儿来的（§7.2(6)）。"""
    return {test_id for test_id, case in report.cases.items() if case.status is TestStatus.PASSED}


# ══════════════════════════════════════════════════════════════
# 流水线
# ══════════════════════════════════════════════════════════════


class _Pipeline:
    """一次验证的可变状态。

    写成类是为了让八步各自是一个短方法：写成一个函数的话，
    要么在几百行里传一大串局部变量，要么用一堆嵌套 try，两种都不好读。
    """

    def __init__(
        self,
        request: ValidationRequest,
        *,
        store: ArtifactStore | None,
        client: Any,
        run_container: Callable[[ContainerSpec], ContainerResult] | None,
    ) -> None:
        self.request = request
        self.plan = request.plan
        self.store = store
        self.client = client
        self.run_container = run_container

        self.started_at = _now()
        self.stamp = self.started_at.strftime(_STAMP_FORMAT)
        self.steps: list[StepResult] = []
        self.artifacts: dict[str, ArtifactRef] = {}
        self.evidence: dict[str, Any] = {}
        self.image_info: ImageInfo | None = None
        self.baseline: ExecutionOutcome | None = None
        self.gold_runs: list[ExecutionOutcome] = []

    # ── 记账 ────────────────────────────────────────────────

    def _record(self, step: str, name: str, ok: bool, detail: str, started: datetime) -> None:
        self.steps.append(
            StepResult(
                step=step,
                name=name,
                ok=ok,
                detail=detail,
                duration_ms=int((_now() - started).total_seconds() * 1000),
            )
        )

    def _store_text(self, key: str, text: str, *, content_type: str) -> None:
        """存一份文本制品。空的不存，存失败只记警告。

        存不下制品不该让验证结论作废：结论已经算出来了，为了一个日志文件把它丢掉
        是本末倒置。但证据缺了要在日志里看得见。
        """
        if self.store is None or not text:
            return
        try:
            ref = self.store.put(key, text.encode("utf-8"), content_type=content_type)
            self.artifacts[key] = ref
        except Exception as exc:  # 存制品失败不该影响验证结论
            logger.warning("验证证据落盘失败", key=key, error=str(exc))

    def artifact_key(self, name: str) -> str:
        """制品 key，按 `07-platform-architecture.md` §17.2 的命名规范拼。"""
        return f"tasks/{self.request.task_id}/validation/{self.stamp}/{name}"

    def _save_run(self, label: str, outcome: ExecutionOutcome, workspace_dir: Path) -> None:
        """把一次容器运行的原始证据落盘：junit 报告 + 容器的两条输出流。

        §7.3 要求"每步的 stdout/stderr/report 全部存为制品，任务详情页可查"——
        只存一个结论、拿不出当时的报告，"题目本身也要可审计"就是空话。
        """
        report_path = workspace_dir / self.plan.test_report_path
        if report_path.is_file():
            self._store_text(
                self.artifact_key(f"{label}.junit.xml"),
                report_path.read_text(encoding="utf-8", errors="replace"),
                content_type=_XML_CONTENT_TYPE,
            )
        if outcome.container is not None:
            self._store_text(
                self.artifact_key(f"{label}.stdout.log"),
                outcome.container.stdout,
                content_type=_TEXT_CONTENT_TYPE,
            )
            self._store_text(
                self.artifact_key(f"{label}.stderr.log"),
                outcome.container.stderr,
                content_type=_TEXT_CONTENT_TYPE,
            )

    # ── S1 ──────────────────────────────────────────────────

    def s1_mirror(self) -> MirrorManager:
        """镜像仓库在本地可用。不在就按 `repo_url` 拉一份。"""
        started = _now()
        mirrors = MirrorManager(self.request.mirror_root)
        repo_name = self.request.repo_name
        url = self.request.repo_url

        if not mirrors.exists(repo_name):
            if not url or url.startswith("golden://"):
                # golden:// 表示这个仓库是 `make golden` 生成的，没有上游可拉。
                # 硬去 clone 只会得到一条看不懂的 git 报错
                self._record(
                    "S1", "镜像仓库可用", False, f"{mirrors.path_for(repo_name)} 不存在", started
                )
                raise _Stop(reason=TaskInvalidReason.REPO_UNAVAILABLE)
            try:
                mirrors.clone(repo_name, url)
            except (MirrorError, GitError) as exc:
                self._record("S1", "镜像仓库可用", False, f"clone 失败：{exc}", started)
                raise _Stop(reason=TaskInvalidReason.REPO_UNAVAILABLE) from exc

        self._record("S1", "镜像仓库可用", True, f"镜像在 {mirrors.path_for(repo_name)}", started)
        return mirrors

    # ── S2 ──────────────────────────────────────────────────

    def s2_checkout(self, mirrors: MirrorManager) -> None:
        """base_commit 在镜像里，而且能干净地物化出来。

        顺带把 C-43 的证据取到：工作区历史只有一个提交，树哈希等于上游那棵树。
        物化本身会校验树哈希（`workspace._verify`），这里记的是"确实校验过了"。
        """
        started = _now()
        repo_name = self.request.repo_name
        commit = self.plan.base_commit

        if not mirrors.has_commit(repo_name, commit):
            # 只 fetch 一次。再多试也没用：commit 不在上游的引用可达范围里，
            # 重复拉取只会浪费几分钟，而它多半是 base_commit 填错了
            try:
                mirrors.fetch(repo_name)
            except (MirrorError, GitError) as exc:
                logger.info("fetch 失败，按 commit 不存在处理", repo=repo_name, error=str(exc))
            if not mirrors.has_commit(repo_name, commit):
                self._record("S2", "物化到 base_commit", False, f"镜像里没有 {commit}", started)
                raise _Stop(reason=TaskInvalidReason.COMMIT_MISSING)

        probe = self.request.scratch_dir / "s2-probe"
        try:
            workspace = materialize_workspace(
                mirror_path=mirrors.path_for(repo_name), base_commit=commit, dest=probe
            )
            commit_count = workspace.commit_count()
            tree_sha = workspace.tree_sha
            file_count = workspace.file_count
        except (WorkspaceError, GitError, OSError) as exc:
            self._record("S2", "物化到 base_commit", False, f"物化失败：{exc}", started)
            raise _Stop(reason=TaskInvalidReason.COMMIT_MISSING) from exc
        finally:
            shutil.rmtree(probe, ignore_errors=True)

        self.evidence["workspace"] = {
            "commit_count": commit_count,
            "tree_sha": tree_sha,
            "file_count": file_count,
        }
        if commit_count != 1:
            # 工作区里留了 base 之后的历史 = 被测 AI 一句 git log 就能翻到官方修复（C-43）
            self._record(
                "S2",
                "物化到 base_commit",
                False,
                f"工作区有 {commit_count} 个提交，应该只有 1 个",
                started,
            )
            raise _Stop(review=f"物化后工作区有 {commit_count} 个提交，防泄题前提不成立（C-43）")

        self._record(
            "S2",
            "物化到 base_commit",
            True,
            f"{file_count} 个文件，历史只有 1 个提交，树 {tree_sha[:12]}",
            started,
        )

    # ── S3 ──────────────────────────────────────────────────

    def s3_image(self) -> None:
        """环境镜像在本地，并取到它的 digest（协议 C-36）。

        §7.3 的 S3 是"build/reuse env image + install"，这里**只做 reuse 那一半**：
        镜像分层构建是 E2-T3，还没做。装依赖同理，按 ADR-008 它属于建镜像的时候，
        而且测试阶段断网（C-31），容器里也装不了。
        """
        started = _now()
        try:
            self.image_info = inspect_image(self.request.image, client=self.client)
        except ImageNotFoundError as exc:
            self._record(
                "S3",
                "环境镜像可用",
                False,
                f"{self.request.image} 不在本地。先 `make images` 建好（分层构建是 E2-T3）",
                started,
            )
            raise _Stop(reason=TaskInvalidReason.ENV_UNBUILDABLE) from exc
        except SandboxError as exc:
            # 连不上 docker 是平台自己的问题，不能算题目坏了
            self._record("S3", "环境镜像可用", False, f"查镜像失败：{exc}", started)
            raise _Stop(error=f"查镜像 {self.request.image} 失败：{exc}") from exc

        self.evidence["image"] = {
            "tag": self.image_info.tag,
            "image_id": self.image_info.image_id,
            "digest": self.image_info.digest,
        }
        digest = self.image_info.digest or "（本地构建，没有 RepoDigest）"
        self._record("S3", "环境镜像可用", True, f"{self.image_info.tag} digest={digest}", started)

    # ── 跑一轮测试 ───────────────────────────────────────────

    def _run_suite(
        self, label: str, patch: str, *, step: str, name: str, record: bool = True
    ) -> ExecutionOutcome:
        """在容器里跑一遍**全量**套件，落证据，把平台故障和题目问题分开。

        走 `execute_tests` 是刻意的：正式评测走的就是这个函数，两边共用同一套
        容器规格、断网策略、补丁应用顺序和报告解析。

        `record=False` 表示这一轮跑成功了也不单独记一条 step —— 复跑是 S8 的内部动作，
        每跑一遍就记一条的话，证据里会冒出好几条 S8，反而看不清。
        **失败照记**：失败要看得见是在哪一遍出的。
        """
        started = _now()
        workspace_dir = self.request.scratch_dir / label
        outcome = execute_tests(
            self.plan,
            patch,
            mirror_path=MirrorManager(self.request.mirror_root).path_for(self.request.repo_name),
            workspace_dir=workspace_dir,
            image=self.request.image,
            # 空序列 = 跑全量。§7.3 的 S4 要的就是全量基线，
            # 只跑 F2P ∪ P2P 子集拿不到 P2P 候选池，也看不见套件里别的用例。
            # None = 让执行器用 plan.test_ids（F2P ∪ P2P），只有 suite_scope=declared 才走这条
            test_ids=() if self.request.suite_scope == "full" else None,
            run_id=f"validate-{self.request.task_id}",
            client=self.client,
            run_container=self.run_container,
        )
        self._save_run(label, outcome, workspace_dir)

        if outcome.infra_outcome is InfraOutcome.TEST_TIMEOUT:
            budget = self.plan.test_timeout_s
            self._record(step, name, False, f"全量套件超过 {budget} 秒还没跑完", started)
            raise _Stop(reason=TaskInvalidReason.TEST_TOO_SLOW)
        if outcome.infra_outcome in PLATFORM_FAULTS:
            self._record(step, name, False, f"平台故障：{outcome.problem}", started)
            fault = outcome.infra_outcome.value
            raise _Stop(error=f"{label} 这一轮平台出故障（{fault}）：{outcome.problem}")
        if outcome.report is None:
            # PATCH_APPLY_FAILED / TEST_DISCOVERY_ERROR 落在这里：补丁打不上说明题目
            # 自己有问题，但 §7.3 的七个 code 没有对应项，硬套一个只会误导人
            self._record(
                step, name, False, f"{outcome.infra_outcome.value}：{outcome.problem}", started
            )
            raise _Stop(review=f"{label}：{outcome.problem}（{outcome.infra_outcome.value}）")

        if not outcome.report.cases:
            # 一条用例都没收集到 —— 套件根本没跑起来。§7.3 的七个 code 没有对应项：
            # 硬套 `F2P_NOT_FAILING` 是在编，因为那句话的意思是"这些测试在修复前就通过了"，
            # 而这里的事实是"这些测试压根没执行"。**两者的处置完全相反**：
            # 前者该丢题，后者该修环境。
            #
            # 2026-09-10 实测（E8-T2 探测 click 2024 年的 base commit）：pytest 9 对
            # `parametrize` 收到 `itertools.chain` 报弃用警告，而 click 自己的 pyproject
            # 写了 `filterwarnings = ["error"]`，警告升成收集期错误；pytest **一个文件收集
            # 出错就中断整轮**，junit 里零条用例。当时十条候选全被判成 F2P_NOT_FAILING，
            # 看起来像"这十道题都是坏题"，其实一道都没问题。
            tail = (outcome.container.stdout or "")[-800:] if outcome.container else ""
            self._record(step, name, False, f"报告里一条用例都没有；输出尾部：{tail}", started)
            raise _Stop(review=f"{label}：套件一条用例都没收集到，多半是环境跑不起来，不是题目坏了")

        integrity = outcome.report.check_integrity((), repo_root=WORKSPACE_TARGET)
        if not integrity.report_complete:
            # 报告残缺（junit 没生成、只能从 stdout 里捞）时判不了题目好坏。
            # §7.2(4) 要求 test_command 必须输出机器可解析的逐用例报告，
            # 所以这多半是题目的 test_command 写错了，但也可能是容器被杀
            self._record(step, name, False, f"报告不完整：{integrity.report_problem}", started)
            raise _Stop(review=f"{label} 的测试报告不完整：{integrity.report_problem}")

        if record:
            scope = "全量" if self.request.suite_scope == "full" else "声明的"
            self._record(
                step, name, True, f"{scope} {len(outcome.report.cases)} 条用例，报告完整", started
            )
        return outcome

    # ── S4 ──────────────────────────────────────────────────

    def s4_baseline(self) -> None:
        """base + test_patch 上跑全量，记下基线。这一轮等价于 Noop 哨兵。"""
        outcome = self._run_suite(
            "s4-baseline", "", step="S4", name="基线：base + test_patch 跑全量"
        )
        self.baseline = outcome
        report = outcome.report
        assert report is not None  # _run_suite 已经保证了
        duration_ms = int((outcome.container.duration_s if outcome.container else 0.0) * 1000)

        self.evidence["baseline"] = {
            "total_cases": len(report.cases),
            "duration_ms": duration_ms,
            "statuses": {
                test_id: case.status.value for test_id, case in sorted(report.cases.items())
            },
            "fail_to_pass": _statuses(report, self.plan.fail_to_pass),
            "pass_to_pass": _statuses(report, self.plan.pass_to_pass),
            "p2p_candidate_pool": sorted(_passing_cases(report)),
            "xpass_may_read_as_passed": report.xpass_may_read_as_passed,
        }

        # 声明的 P2P 在基线上就必须全过（§7.2(6)）。放在这儿查而不是留到 S8，
        # 是因为 S8 的 GOLD_REGRESSION 意思是"gold 把它打挂了"——
        # 一条在 base 上就挂的用例记成 gold 的账，是错误的诊断
        broken = {
            test_id: status
            for test_id, status in self.evidence["baseline"]["pass_to_pass"].items()
            if status != TestStatus.PASSED.value
        }
        if broken:
            raise _Stop(review=f"这些 P2P 在 base + test_patch 上就没通过：{broken}")

    # ── S5 ──────────────────────────────────────────────────

    def s5_f2p_fails(self) -> None:
        """每一条 F2P 在基线里都必须是 FAILED 或 ERROR。

        **逐条判，不是"至少一条"。** 漏掉一条在修复前就通过的 F2P，
        Noop 哨兵就会给出非零解决率，协议 C-50 的发布门槛当场失效。
        """
        started = _now()
        assert self.baseline is not None and self.baseline.report is not None
        report = self.baseline.report

        not_failing: dict[str, str] = {}
        for test_id in self.plan.fail_to_pass:
            status = status_of(report, test_id)
            if status not in FAILING_STATUSES:
                not_failing[test_id] = status.value

        if not_failing:
            missing = [t for t, s in not_failing.items() if s == TestStatus.MISSING.value]
            detail = f"这些 F2P 在修复前没有失败：{not_failing}"
            if missing:
                # 假 MISSING 的头号成因是用例 ID 归一化写错（AGENTS.md §5.5）。
                # 直接把"长得几乎一样"的报告用例指出来，省得人拿两列 ID 肉眼比
                check = report.check_integrity(missing, repo_root=WORKSPACE_TARGET)
                detail += f"；其中 {len(missing)} 条报告里根本没有，相近用例：{check.near_misses}"
            self._record("S5", "F2P 在基线上全挂", False, detail, started)
            raise _Stop(reason=TaskInvalidReason.F2P_NOT_FAILING)

        self._record(
            "S5",
            "F2P 在基线上全挂",
            True,
            f"{len(self.plan.fail_to_pass)} 条 F2P 全部失败",
            started,
        )

    # ── S6 / S7 / S8 ────────────────────────────────────────

    def s6_s7_gold(self) -> None:
        """打上 gold_patch 再跑全量，F2P 必须全部通过。这一轮等价于 Oracle 哨兵。"""
        started = _now()
        outcome = self._run_suite(
            "s7-gold", self.request.gold_patch, step="S6", name="打上 gold_patch"
        )
        self.gold_runs.append(outcome)
        report = outcome.report
        assert report is not None
        duration_ms = int((outcome.container.duration_s if outcome.container else 0.0) * 1000)

        self.evidence["after_gold"] = {
            "total_cases": len(report.cases),
            "duration_ms": duration_ms,
            "fail_to_pass": _statuses(report, self.plan.fail_to_pass),
            "pass_to_pass": _statuses(report, self.plan.pass_to_pass),
            # 和 S4 的那份对称。§7.2(6) 把 P2P 定义成"两边都通过"，所以派生 P2P
            # （E8-T2）要的是这两份的**交集** —— 只用 S4 那一半的话，凡是 gold
            # 顺带改了行为的用例都会在 S8 被记成 GOLD_REGRESSION，好题被丢掉，
            # 而且理由是错的：gold 没有回归，是我们把不该当护栏的用例塞进了护栏。
            "p2p_candidate_pool": sorted(_passing_cases(report)),
        }

        not_passing = {
            test_id: status
            for test_id, status in self.evidence["after_gold"]["fail_to_pass"].items()
            if status != TestStatus.PASSED.value
        }
        if not_passing:
            self._record(
                "S7", "F2P 打完 gold 全过", False, f"这些 F2P 仍然没通过：{not_passing}", started
            )
            raise _Stop(reason=TaskInvalidReason.GOLD_NOT_FIXING)

        self._record(
            "S7",
            "F2P 打完 gold 全过",
            True,
            f"{len(self.plan.fail_to_pass)} 条 F2P 全部通过",
            started,
        )

    def s8_regression(self) -> None:
        """P2P 不能被 gold_patch 打挂；再跑一遍找不稳定用例。"""
        started = _now()
        after_gold: dict[str, str] = self.evidence["after_gold"]["pass_to_pass"]
        regressed = {
            test_id: status
            for test_id, status in after_gold.items()
            if status != TestStatus.PASSED.value
        }
        if regressed:
            # 到这儿能确定是 gold 干的：S4 已经验过这些用例在 base 上是通过的
            self._record(
                "S8", "P2P 仍然全过", False, f"gold_patch 打挂了这些 P2P：{regressed}", started
            )
            raise _Stop(reason=TaskInvalidReason.GOLD_REGRESSION)

        unstable = self._rerun_for_flaky()
        self.evidence["flaky"] = unstable

        if unstable["fail_to_pass"]:
            # F2P 时过时不过就等于"没稳定修好"，题目不能要（§7.2(7)）
            self._record(
                "S8",
                "P2P 仍然全过",
                False,
                f"这些 F2P 复跑结果不一致：{unstable['fail_to_pass']}",
                started,
            )
            raise _Stop(reason=TaskInvalidReason.GOLD_NOT_FIXING)

        detail = f"{len(self.plan.pass_to_pass)} 条 P2P 全部通过"
        if unstable["pass_to_pass"]:
            self._record("S8", "P2P 仍然全过", False, f"{detail}，但复跑发现不稳定用例", started)
            raise _Stop(
                review=f"这些 P2P 复跑结果不一致，按 §7.2(7) 应当剔除：{unstable['pass_to_pass']}"
            )
        self._record(
            "S8", "P2P 仍然全过", True, f"{detail}，复跑 {self.request.repeat} 遍结果一致", started
        )

    def _rerun_for_flaky(self) -> dict[str, Any]:
        """gold 侧再跑几遍，找出前后状态不一致的用例。

        只对 gold 侧复跑：F2P 的"必须通过"和 P2P 的"必须仍然通过"两条断言都在这一侧，
        基线侧的抖动会表现成"F2P 有时候通过"，那由 S5 当场拦下。
        再多跑一遍基线会让最贵的一步再贵一倍，收益却小得多。
        """
        result: dict[str, Any] = {"runs": 1, "fail_to_pass": {}, "pass_to_pass": {}, "other": 0}
        if self.request.repeat < 2:
            return result

        first = self.gold_runs[0].report
        assert first is not None
        declared = (*self.plan.fail_to_pass, *self.plan.pass_to_pass)

        for index in range(2, self.request.repeat + 1):
            outcome = self._run_suite(
                f"s8-rerun-{index}",
                self.request.gold_patch,
                step="S8",
                name=f"复跑第 {index} 遍",
                record=False,
            )
            self.gold_runs.append(outcome)
            again = outcome.report
            assert again is not None

            for test_id in declared:
                before, now = status_of(first, test_id), status_of(again, test_id)
                if before is now:
                    continue
                bucket = "fail_to_pass" if test_id in self.plan.fail_to_pass else "pass_to_pass"
                result[bucket][test_id] = f"{before.value} → {now.value}"

            # 名单之外的用例还有多少条状态变了。只计数不列名：它们不参与判定，
            # 但数字不为 0 说明这个套件整体就不稳，值得看一眼
            declared_norm = {normalize_test_id(t) for t in declared}
            result["other"] += sum(
                1
                for test_id in set(first.cases) & set(again.cases)
                if test_id not in declared_norm
                and first.cases[test_id].status is not again.cases[test_id].status
            )
            result["runs"] = index

        return result


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


def validate_task(
    request: ValidationRequest,
    *,
    store: ArtifactStore | None = None,
    client: Any = None,
    run_container: Callable[[ContainerSpec], ContainerResult] | None = None,
) -> ValidationResult:
    """跑 §7.3 的八步，返回结论和证据。**不抛异常**（除非调用方传错参数）。

    `store` 为 None 时不落制品（单测用），真实验证必须给 —— §7.3 要求
    "任务本身也要可审计"，没有制品就谈不上审计。

    `run_container` 是给测试用的接缝，原样透传给 `execute_tests`：传一个假的进来，
    就能在**不起容器**的前提下验完八步的判定逻辑和七个 reason code。
    """
    pipeline = _Pipeline(request, store=store, client=client, run_container=run_container)
    state: TaskValidationState | None = TaskValidationState.VALID
    reason: TaskInvalidReason | None = None
    error: str | None = None
    review: str | None = None

    try:
        mirrors = pipeline.s1_mirror()
        pipeline.s2_checkout(mirrors)
        pipeline.s3_image()
        pipeline.s4_baseline()
        pipeline.s5_f2p_fails()
        pipeline.s6_s7_gold()
        pipeline.s8_regression()
    except _Stop as stop:
        reason, review, error = stop.reason, stop.review, stop.error
        if error is not None:
            state = None
        elif reason is not None:
            # 已经是 VALID 的题复验没过 → 隔离（§7.4）。首次验证没过 → 无效。
            # 协议 C-20a 禁止的是"正式评测超时一次就隔离"，那是 E4-T5 的对照组流程，
            # 和这里不是一回事
            state = (
                TaskValidationState.QUARANTINED
                if request.previous_state is TaskValidationState.VALID
                else TaskValidationState.INVALID
            )
        else:
            state = TaskValidationState.REVIEW_REQUIRED
    except Exception as exc:  # 兜底：异常冒出去会让题目永远卡在 VALIDATING
        logger.exception("验证流水线自己出错了", task_id=request.task_id)
        state, error = None, f"验证流水线自己出错了：{exc}"

    if state is TaskValidationState.VALID:
        # 八步全过之后还要看两类"合法但值得人看一眼"的信号（§7.4）：
        # 题目自身的 review_flags（issue 太短、F2P 过多、P2P 为空），
        # 以及基线耗时逼近超时阈值
        flags = [*request.review_flags]
        slow = _slow_flag(pipeline)
        if slow:
            flags.append(slow)
        if flags:
            review = "；".join(flags)
            state = TaskValidationState.REVIEW_REQUIRED

    finished_at = _now()
    evidence = _build_evidence(pipeline, state, reason, review, error, finished_at)
    evidence_ref = _store_evidence(pipeline, evidence)

    logger.info(
        "题目验证完成",
        task_id=request.task_id,
        state=state.value if state else None,
        reason_code=reason.value if reason else None,
    )
    return ValidationResult(
        state=state,
        reason_code=reason,
        steps=tuple(pipeline.steps),
        evidence=evidence,
        artifacts=dict(pipeline.artifacts),
        evidence_uri=evidence_ref.uri if evidence_ref else None,
        error=error,
        validated_at=pipeline.started_at,
        image=pipeline.image_info,
    )


def _timing_baseline_ms(pipeline: _Pipeline) -> int | None:
    """耗时基线：基线那一轮跑全量套件用了多久。§7.3 要求 VALID 时写下它。"""
    baseline = pipeline.evidence.get("baseline")
    return None if baseline is None else int(baseline["duration_ms"])


def _slow_flag(pipeline: _Pipeline) -> str | None:
    """基线耗时逼近超时阈值就提请复核（§7.4）。"""
    duration_ms = _timing_baseline_ms(pipeline)
    budget_ms = pipeline.plan.test_timeout_s * 1000
    if duration_ms is None or duration_ms < budget_ms * SLOW_TEST_RATIO:
        return None
    return (
        f"基线耗时 {duration_ms} ms，已达 test_timeout_s（{budget_ms} ms）的 {SLOW_TEST_RATIO:.0%}"
    )


def _build_evidence(
    pipeline: _Pipeline,
    state: TaskValidationState | None,
    reason: TaskInvalidReason | None,
    review: str | None,
    error: str | None,
    finished_at: datetime,
) -> dict[str, Any]:
    """拼证据文档。**结论和过程放在同一份文件里**，事后不用对着两处拼故事。"""
    request = pipeline.request
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "task_id": request.task_id,
        "content_hash": request.content_hash,
        "started_at": pipeline.started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "state": state.value if state else None,
        "reason_code": reason.value if reason else None,
        "review": review,
        "error": error,
        "review_flags": list(request.review_flags),
        "previous_state": request.previous_state.value if request.previous_state else None,
        "repeat": request.repeat,
        "timing_baseline_ms": _timing_baseline_ms(pipeline),
        "steps": [
            {
                "step": s.step,
                "name": s.name,
                "ok": s.ok,
                "detail": s.detail,
                "duration_ms": s.duration_ms,
            }
            for s in pipeline.steps
        ],
        "task": {
            "base_commit": pipeline.plan.base_commit,
            "fail_to_pass": list(pipeline.plan.fail_to_pass),
            "pass_to_pass": list(pipeline.plan.pass_to_pass),
            "test_command": pipeline.plan.test_command,
            "test_timeout_s": pipeline.plan.test_timeout_s,
            "suite_scope": request.suite_scope,
        },
        # 各步自己攒下的证据（workspace / image / baseline / after_gold / flaky）
        # 平铺到顶层，读证据的人不用先猜它们藏在哪个子对象里
        **dict(pipeline.evidence),
    }


def _store_evidence(pipeline: _Pipeline, evidence: Mapping[str, Any]) -> ArtifactRef | None:
    """把证据文档落盘，返回它的引用（回填 `validation_evidence_uri` 用）。"""
    if pipeline.store is None:
        return None
    key = pipeline.artifact_key("evidence.json")
    text = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    try:
        ref = pipeline.store.put(key, text.encode("utf-8"), content_type=_JSON_CONTENT_TYPE)
    except Exception as exc:  # 存不下证据也要把结论给出去
        logger.warning("验证证据文档落盘失败", key=key, error=str(exc))
        return None
    # 也登记进 artifacts：调用方按这张表往 `artifacts` 表建索引行，
    # 漏了它就会出现"validation_evidence_uri 指向一个库里查不到的制品"
    pipeline.artifacts[key] = ref
    return ref


def summarize(results: Sequence[tuple[str, ValidationResult]]) -> str:
    """把一批结果拼成一段人能读的摘要，CLI 和测试共用。"""
    lines = []
    for task_id, result in results:
        mark = "✓" if result.valid else "✗"
        label = result.state.value if result.state else "没得出结论"
        extra = f"（{result.reason_code.value}）" if result.reason_code else ""
        lines.append(f"{mark} {task_id:34} {label}{extra}")
    valid = sum(1 for _, r in results if r.valid)
    lines.append(f"\n共 {len(results)} 道，VALID {valid}，其余 {len(results) - valid}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_REPEAT",
    "EVIDENCE_SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "SLOW_TEST_RATIO",
    "StepResult",
    "ValidationRequest",
    "ValidationResult",
    "status_of",
    "summarize",
    "validate_task",
]
