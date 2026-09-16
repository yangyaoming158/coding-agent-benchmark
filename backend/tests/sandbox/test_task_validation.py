"""题目验证流水线的判定逻辑（E1-T3，`03-benchmark-spec.md` §7.3）。

**这一组不用 Docker**，`make test` 每次都会跑。做法是把测试报告喂进去：
`execute_tests` 留了 `run_container` 这个接缝，传一个假的容器执行器进来，
它按脚本往工作区里写 junit 报告，八步的判定逻辑就能在几秒内验完。

真起容器的那一层在 `test_task_validation_docker.py`，它证明的是另一件事 ——
这套逻辑接上真 pytest 之后仍然成立。两层都要有：只有假的证明不了链路通，
只有真的跑得太慢、而且构造不出 OOM 这类边界情况。

## 假的是容器，不是别的

工作区是**真的**（`git archive` 从真镜像物化）、补丁是**真的**（`git apply` 打上去）、
报告解析是**真的**。假的只有"跑 pytest"这一步 —— 也只有这一步需要 Docker。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import ImageNotFound

from app.benchmark.schema import TaskDefinition
from app.domain.enums import TaskInvalidReason, TaskValidationState
from app.domain.enums import TestStatus as Status
from app.evaluation.validation import (
    ValidationRequest,
    ValidationResult,
    validate_task,
)
from app.sandbox.container import ContainerResult, ContainerSpec
from app.storage.local import LocalArtifactStore
from cli.golden import build

TASK_ID = "bench-golden__textkit-1"
REPO_NAME = "bench-golden/textkit"
IMAGE = "bench-golden:py311"

#: 这道题在 base 上的真实状态：三条 F2P 挂、四条 P2P 过。
#: 写死而不是去跑一遍 —— 这一组要验的是"拿到这样一份报告之后怎么判"。
BASE_STATUSES: dict[str, Status] = {
    "tests/test_csvline.py::test_quoted_field_keeps_comma": Status.FAILED,
    "tests/test_csvline.py::test_quoted_field_in_the_middle": Status.FAILED,
    "tests/test_csvline.py::test_double_quote_inside_quoted_field": Status.FAILED,
    "tests/test_csvline.py::test_splits_plain_fields": Status.PASSED,
    "tests/test_csvline.py::test_keeps_empty_fields": Status.PASSED,
    "tests/test_csvline.py::test_single_field": Status.PASSED,
    "tests/test_csvline.py::test_strips_trailing_newline": Status.PASSED,
}
#: 打完 gold 之后：七条全过。
FIXED_STATUSES: dict[str, Status] = dict.fromkeys(BASE_STATUSES, Status.PASSED)

A_F2P = "tests/test_csvline.py::test_quoted_field_keeps_comma"
A_P2P = "tests/test_csvline.py::test_splits_plain_fields"


# ══════════════════════════════════════════════════════════════
# 假容器
# ══════════════════════════════════════════════════════════════


def junit_xml(statuses: Mapping[str, Status]) -> str:
    """按 `{用例 ID: 状态}` 拼一份 junitxml。

    带上 `file=` 属性（xunit1 的写法）：解析器靠它拿到用例的文件路径，
    没有它就只能从 `classname` 猜，而猜不准正是假 `MISSING` 的来源。
    """
    suite = ET.Element(
        "testsuite",
        {"name": "pytest", "tests": str(len(statuses)), "errors": "0", "failures": "0"},
    )
    for test_id, status in statuses.items():
        path, _, node = test_id.partition("::")
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": path.removesuffix(".py").replace("/", "."),
                "name": node,
                "file": path,
                "time": "0.01",
            },
        )
        if status is Status.FAILED:
            ET.SubElement(case, "failure", {"message": "assert False"}).text = "AssertionError"
        elif status is Status.ERROR:
            ET.SubElement(case, "error", {"message": "boom"}).text = "RuntimeError"
        elif status is Status.SKIPPED:
            ET.SubElement(case, "skipped", {"type": "pytest.skip", "message": "跳过"})
    root = ET.Element("testsuites")
    root.append(suite)
    return ET.tostring(root, encoding="unicode")


@dataclass
class FakeRun:
    """一次假的容器运行：写哪份报告出来、以什么方式结束。"""

    statuses: Mapping[str, Status] | None = None
    timed_out: bool = False
    oom_killed: bool = False
    #: 报告干脆不写（模拟容器被杀、test_command 写错）。
    write_report: bool = True
    #: 这一轮跑了多久。耗时基线和"逼近超时阈值"的复核判断都看它。
    duration_s: float = 0.5


@dataclass
class FakeContainers:
    """按脚本回放的容器执行器。脚本用完之后一直重复最后一条。

    重复最后一条是为了让"复跑 N 遍"的用例好写：给两条脚本就表示
    "第一遍这样、之后都那样"。
    """

    script: list[FakeRun]
    calls: list[ContainerSpec] = field(default_factory=list)

    def __call__(self, spec: ContainerSpec) -> ContainerResult:
        run = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append(spec)

        if run.write_report and run.statuses is not None:
            workspace = Path(spec.mounts[0].source)
            report = workspace / "report" / "junit.xml"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(junit_xml(run.statuses), encoding="utf-8")

        failed = run.statuses is not None and any(
            s is not Status.PASSED for s in run.statuses.values()
        )
        return ContainerResult(
            container_id="fake",
            image=spec.image,
            # pytest 有用例挂就非零退出。测试阶段非零是正常的，不是故障
            exit_code=1 if failed else 0,
            oom_killed=run.oom_killed,
            timed_out=run.timed_out,
            duration_s=run.duration_s,
            stdout="fake pytest stdout",
            stderr="",
        )


class _FakeImages:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    def get(self, name: str) -> Any:
        if name not in self.known:
            raise ImageNotFound(f"no such image: {name}")

        return SimpleNamespace(
            id="sha256:" + "ab" * 32,
            attrs={"RepoDigests": ["bench-golden/py311@sha256:" + "cd" * 32]},
        )


class FakeDocker:
    """只实现 `images.get` —— `inspect_image` 用得到的就这一个。"""

    def __init__(self, known: set[str] = frozenset({IMAGE})) -> None:  # type: ignore[assignment]
        self.images = _FakeImages(set(known))


# ══════════════════════════════════════════════════════════════
# 固定装置
# ══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> tuple[TaskDefinition, Path]:
    """把 textkit 那道题连同它的镜像仓库现建一份，返回题目和镜像根目录。

    现建而不是用 `var/mirrors/` 下那份：那个目录在 .gitignore 里，
    换台机器就是空的，测试不该依赖"你之前跑过 make golden"。
    """
    mirror_root = tmp_path_factory.mktemp("mirrors")
    output = build([TASK_ID], mirror_root=mirror_root)
    return TaskDefinition.model_validate_json(output.tasks[TASK_ID]), mirror_root


def make_request(
    golden: tuple[TaskDefinition, Path], scratch: Path, **overrides: Any
) -> ValidationRequest:
    task, mirror_root = golden
    request = ValidationRequest(
        plan=task.execution_plan(),
        gold_patch=task.gold_patch,
        repo_name=REPO_NAME,
        mirror_root=mirror_root,
        scratch_dir=scratch,
        image=IMAGE,
        repo_url=task.repo_url,
        content_hash=task.content_hash,
    )
    return replace(request, **overrides) if overrides else request


def run_pipeline(
    request: ValidationRequest,
    script: list[FakeRun],
    *,
    store: LocalArtifactStore | None = None,
    images: set[str] = frozenset({IMAGE}),  # type: ignore[assignment]
) -> tuple[ValidationResult, FakeContainers]:
    containers = FakeContainers(script)
    result = validate_task(
        request, store=store, client=FakeDocker(images), run_container=containers
    )
    return result, containers


HEALTHY = [FakeRun(BASE_STATUSES), FakeRun(FIXED_STATUSES)]


# ══════════════════════════════════════════════════════════════
# 八步全过
# ══════════════════════════════════════════════════════════════


def test_healthy_task_is_valid(golden: Any, tmp_path: Path) -> None:
    result, containers = run_pipeline(make_request(golden, tmp_path), HEALTHY)

    assert result.state is TaskValidationState.VALID
    assert result.reason_code is None
    assert result.error is None
    assert [s.step for s in result.steps] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
    assert all(s.ok for s in result.steps)
    # S4 基线、S7 打 gold、S8 复跑 —— 一共三次，S5 不该再起容器
    assert len(containers.calls) == 3


def test_s5_reads_the_baseline_instead_of_rerunning(golden: Any, tmp_path: Path) -> None:
    """S5 逐条判 F2P，但**不额外起容器**：状态是从 S4 的全量报告里读的。

    这是本实现和 `cli/golden.py` 六步验证最大的区别。逐条起 pytest 的话，
    F2P 有 20 条就要跑 20 遍，而全量报告里本来就有这 20 条的状态。
    """
    result, containers = run_pipeline(make_request(golden, tmp_path, repeat=1), HEALTHY)

    assert result.state is TaskValidationState.VALID
    assert len(containers.calls) == 2  # repeat=1 时没有复跑
    # 每次跑的都是全量套件：命令里不该出现具体的用例 ID
    for spec in containers.calls:
        assert not [arg for arg in spec.command if "::" in arg]


def test_declared_scope_runs_only_f2p_and_p2p(golden: Any, tmp_path: Path) -> None:
    """`suite_scope=declared`（E1-T7）：三轮都只跑题目声明的用例，证据里记下这一点。

    给 P2P 已经由官方定好的题用（SWE-bench 导入）—— 它们不需要从全量报告里派生候选池，
    而全量套件动辄几十分钟。
    """
    request = make_request(golden, tmp_path, suite_scope="declared")
    result, containers = run_pipeline(request, HEALTHY)

    assert result.state is TaskValidationState.VALID
    declared = set(request.plan.fail_to_pass) | set(request.plan.pass_to_pass)
    for spec in containers.calls:
        # 命令行尾巴上就是 F2P ∪ P2P 的用例 ID，一条不多一条不少
        assert {arg for arg in spec.command if "::" in arg} == declared
    assert result.evidence["task"]["suite_scope"] == "declared"
    assert any("声明的" in s.detail for s in result.steps if s.step == "S4")


def test_full_scope_is_the_default_and_recorded(golden: Any, tmp_path: Path) -> None:
    result, _ = run_pipeline(make_request(golden, tmp_path), HEALTHY)
    assert result.evidence["task"]["suite_scope"] == "full"
    assert result.evidence["schema_version"] == "1.1"


def test_baseline_evidence_has_full_case_list(golden: Any, tmp_path: Path) -> None:
    """VALID 时要写下"用例清单 + 耗时基线 + 镜像 digest"（§7.3 最后一行）。"""
    result, _ = run_pipeline(make_request(golden, tmp_path), HEALTHY)

    baseline = result.evidence["baseline"]
    assert baseline["total_cases"] == len(BASE_STATUSES)
    assert baseline["statuses"][A_F2P] == Status.FAILED.value
    # P2P 候选池 = 基线上通过的全部用例（§7.2(6)）
    assert set(baseline["p2p_candidate_pool"]) == {
        t for t, s in BASE_STATUSES.items() if s is Status.PASSED
    }
    assert result.evidence["timing_baseline_ms"] == baseline["duration_ms"]
    assert result.evidence["image"]["digest"] == "sha256:" + "cd" * 32
    assert result.image is not None and result.image.digest


def test_workspace_evidence_proves_history_stripped(golden: Any, tmp_path: Path) -> None:
    """S2 顺带留下防泄题的证据：工作区历史只有一个提交（协议 C-43）。"""
    result, _ = run_pipeline(make_request(golden, tmp_path), HEALTHY)

    assert result.evidence["workspace"]["commit_count"] == 1
    assert len(result.evidence["workspace"]["tree_sha"]) == 40


# ══════════════════════════════════════════════════════════════
# 七种坏任务
# ══════════════════════════════════════════════════════════════


def test_repo_unavailable(golden: Any, tmp_path: Path) -> None:
    """镜像仓库不在本地，而 `golden://` 没有上游可拉。"""
    request = make_request(golden, tmp_path, mirror_root=tmp_path / "empty")
    result, containers = run_pipeline(request, HEALTHY)

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.REPO_UNAVAILABLE
    assert not containers.calls  # 连容器都不该起


def test_commit_missing(golden: Any, tmp_path: Path) -> None:
    task, _ = golden
    request = make_request(golden, tmp_path)
    request = replace(request, plan=replace(request.plan, base_commit="0" * 40))
    result, _ = run_pipeline(request, HEALTHY)

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.COMMIT_MISSING
    assert task.base_commit != "0" * 40  # 确认我们改的是个真不存在的 commit


def test_env_unbuildable(golden: Any, tmp_path: Path) -> None:
    """镜像不在本地。E2-T3 的分层构建器到位前，S3 只做 reuse 这一半。"""
    result, containers = run_pipeline(
        make_request(golden, tmp_path, image="bench-nonexistent:v0"), HEALTHY, images=set()
    )

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.ENV_UNBUILDABLE
    assert "make images" in result.steps[-1].detail
    assert not containers.calls


def test_test_too_slow(golden: Any, tmp_path: Path) -> None:
    """基线全量套件超时 → `TEST_TOO_SLOW`（§7.3 的 S4）。"""
    result, _ = run_pipeline(make_request(golden, tmp_path), [FakeRun(timed_out=True)])

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.TEST_TOO_SLOW


def test_f2p_not_failing(golden: Any, tmp_path: Path) -> None:
    """有一条 F2P 在修复前就通过 —— 这正是 Noop 哨兵会掉到非零的原因。"""
    base = {**BASE_STATUSES, A_F2P: Status.PASSED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(base), FakeRun(FIXED_STATUSES)]
    )

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.F2P_NOT_FAILING
    assert A_F2P in result.steps[-1].detail


def test_gold_not_fixing(golden: Any, tmp_path: Path) -> None:
    """打完官方补丁 F2P 仍然挂 —— Oracle 哨兵会掉到 100% 以下。"""
    fixed = {**FIXED_STATUSES, A_F2P: Status.FAILED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(BASE_STATUSES), FakeRun(fixed)]
    )

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.GOLD_NOT_FIXING


def test_gold_regression(golden: Any, tmp_path: Path) -> None:
    """官方补丁修好了 F2P，却把一条 P2P 打挂了。"""
    fixed = {**FIXED_STATUSES, A_P2P: Status.FAILED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(BASE_STATUSES), FakeRun(fixed)]
    )

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.GOLD_REGRESSION
    assert A_P2P in result.steps[-1].detail


# ══════════════════════════════════════════════════════════════
# 判定的边界
# ══════════════════════════════════════════════════════════════


def test_skipped_f2p_is_not_a_failure(golden: Any, tmp_path: Path) -> None:
    """一条被 skip 掉的 F2P 证明不了 bug 存在（协议 C-12 的反面）。"""
    base = {**BASE_STATUSES, A_F2P: Status.SKIPPED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(base), FakeRun(FIXED_STATUSES)]
    )

    assert result.reason_code is TaskInvalidReason.F2P_NOT_FAILING
    assert Status.SKIPPED.value in result.steps[-1].detail


def test_missing_f2p_points_at_id_normalization(golden: Any, tmp_path: Path) -> None:
    """报告里根本没有这条 F2P 时，要把"长得几乎一样"的用例指出来。

    假 `MISSING` 的头号成因是用例 ID 归一化写错（AGENTS.md §5.5），
    那种 bug 不报错、只让解决率莫名偏低。报错信息里直接给出相近用例，
    是为了让人一眼看出"是 ID 对不上，不是题目坏了"。
    """
    # 只有这一条 F2P 的路径少了 `tests/` 这一层，其余用例都正常。
    # 后缀对不上，`resolve()` 的三层兜底全部落空 —— 这正是真实世界里的假 MISSING
    base = {k: v for k, v in BASE_STATUSES.items() if k != A_F2P}
    base[A_F2P.removeprefix("tests/")] = Status.FAILED
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(base), FakeRun(FIXED_STATUSES)]
    )

    assert result.reason_code is TaskInvalidReason.F2P_NOT_FAILING
    detail = result.steps[-1].detail
    assert Status.MISSING.value in detail
    assert "相近用例" in detail


def test_p2p_failing_on_baseline_is_review_not_regression(golden: Any, tmp_path: Path) -> None:
    """P2P 在 base 上就挂 → 提请复核，**不能**记成 gold 的回归。

    记成 `GOLD_REGRESSION` 是错误的诊断：gold 根本没碰它。
    """
    base = {**BASE_STATUSES, A_P2P: Status.FAILED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(base), FakeRun(FIXED_STATUSES)]
    )

    assert result.state is TaskValidationState.REVIEW_REQUIRED
    assert result.reason_code is None
    assert A_P2P in str(result.evidence["review"])


def test_review_flags_downgrade_valid_to_review(golden: Any, tmp_path: Path) -> None:
    """八步全过，但题目自身有可疑之处（§7.4）→ `REVIEW_REQUIRED`，不是 `VALID`。"""
    request = make_request(golden, tmp_path, review_flags=("pass_to_pass 为空：没有回归护栏",))
    result, _ = run_pipeline(request, HEALTHY)

    assert result.state is TaskValidationState.REVIEW_REQUIRED
    assert "回归护栏" in str(result.evidence["review"])


def test_slow_baseline_asks_for_review(golden: Any, tmp_path: Path) -> None:
    """基线耗时逼近 `test_timeout_s` 也要人看一眼 —— 正式评测很容易踩线超时。"""
    request = make_request(golden, tmp_path)
    # 预算 1 秒，基线跑了 0.9 秒 —— 90%，越过 SLOW_TEST_RATIO 那条线
    request = replace(request, plan=replace(request.plan, test_timeout_s=1))
    result, _ = run_pipeline(
        request,
        [FakeRun(BASE_STATUSES, duration_s=0.9), FakeRun(FIXED_STATUSES)],
    )

    assert result.state is TaskValidationState.REVIEW_REQUIRED
    assert "基线耗时" in str(result.evidence["review"])


def test_report_incomplete_is_review_not_invalid(golden: Any, tmp_path: Path) -> None:
    """junit 没生成时判不了题目好坏，只能提请复核。

    硬套一个 reason code 是在编：报告残缺可能是题目的 `test_command` 写错了
    （§7.2(4) 要求必须输出机器可解析的逐用例报告），也可能是容器被杀。
    """
    result, _ = run_pipeline(
        make_request(golden, tmp_path), [FakeRun(BASE_STATUSES, write_report=False)]
    )

    assert result.state is TaskValidationState.REVIEW_REQUIRED
    assert result.reason_code is None


# ══════════════════════════════════════════════════════════════
# 复跑与不稳定用例
# ══════════════════════════════════════════════════════════════


def test_flaky_p2p_is_reported_not_removed(golden: Any, tmp_path: Path) -> None:
    """P2P 两遍结果不一致 → 报出来 + `REVIEW_REQUIRED`，**不改题目**。

    §7.2(7) 写的是"剔除"，但剔除会改 `pass_to_pass`，进而改 `content_hash`，
    而 `content_hash` 是数据集快照的身份证（§7.5）。所以这里只报不改。
    """
    flaky = {**FIXED_STATUSES, A_P2P: Status.FAILED}
    result, containers = run_pipeline(
        make_request(golden, tmp_path),
        [FakeRun(BASE_STATUSES), FakeRun(FIXED_STATUSES), FakeRun(flaky)],
    )

    assert result.state is TaskValidationState.REVIEW_REQUIRED
    assert result.reason_code is None
    assert A_P2P in result.evidence["flaky"]["pass_to_pass"]
    assert len(containers.calls) == 3


def test_flaky_f2p_is_invalid(golden: Any, tmp_path: Path) -> None:
    """F2P 时过时不过就等于没稳定修好（§7.2(7)）。"""
    flaky = {**FIXED_STATUSES, A_F2P: Status.FAILED}
    result, _ = run_pipeline(
        make_request(golden, tmp_path),
        [FakeRun(BASE_STATUSES), FakeRun(FIXED_STATUSES), FakeRun(flaky)],
    )

    assert result.state is TaskValidationState.INVALID
    assert result.reason_code is TaskInvalidReason.GOLD_NOT_FIXING


def test_repeat_one_skips_the_flaky_check(golden: Any, tmp_path: Path) -> None:
    result, containers = run_pipeline(make_request(golden, tmp_path, repeat=1), HEALTHY)

    assert result.state is TaskValidationState.VALID
    assert result.evidence["flaky"]["runs"] == 1
    assert len(containers.calls) == 2


# ══════════════════════════════════════════════════════════════
# 状态机与平台故障
# ══════════════════════════════════════════════════════════════


def test_revalidation_failure_quarantines(golden: Any, tmp_path: Path) -> None:
    """已经是 VALID 的题复验没过 → `QUARANTINED`（§7.4），不是 `INVALID`。"""
    fixed = {**FIXED_STATUSES, A_F2P: Status.FAILED}
    request = make_request(golden, tmp_path, previous_state=TaskValidationState.VALID)
    result, _ = run_pipeline(request, [FakeRun(BASE_STATUSES), FakeRun(fixed)])

    assert result.state is TaskValidationState.QUARANTINED
    assert result.reason_code is TaskInvalidReason.GOLD_NOT_FIXING


def test_first_timeout_does_not_quarantine(golden: Any, tmp_path: Path) -> None:
    """首次验证超时是 `INVALID(TEST_TOO_SLOW)`，不是隔离（协议 C-20a）。

    C-20a 管的是**已发布题目在正式评测时**超时一次就被隔离 —— 那种情况要先按
    C-20 跑对照组。这里是首次验证，§7.3 的 S4 明写判无效，两者不是一回事。
    """
    result, _ = run_pipeline(make_request(golden, tmp_path), [FakeRun(timed_out=True)])

    assert result.state is TaskValidationState.INVALID
    assert result.state is not TaskValidationState.QUARANTINED


def test_platform_fault_gives_no_verdict(golden: Any, tmp_path: Path) -> None:
    """容器被 OOM 杀掉是平台的问题，不能记成题目无效。

    这条是整套东西的底线：把平台故障写成题目坏了，真正的原因就再没人去查了。
    """
    result, _ = run_pipeline(make_request(golden, tmp_path), [FakeRun(oom_killed=True)])

    assert result.state is None
    assert result.reason_code is None
    assert result.error is not None and "OOM_KILLED" in result.error


def test_docker_unreachable_gives_no_verdict(golden: Any, tmp_path: Path) -> None:
    """连不上 docker 同样不下结论。"""

    class Broken:
        @property
        def images(self) -> Any:
            raise RuntimeError("connection refused")

    result = validate_task(
        make_request(golden, tmp_path), client=Broken(), run_container=FakeContainers(HEALTHY)
    )
    assert result.state is None
    assert result.error is not None


# ══════════════════════════════════════════════════════════════
# 证据制品
# ══════════════════════════════════════════════════════════════


def test_evidence_artifacts_follow_the_key_convention(golden: Any, tmp_path: Path) -> None:
    """制品 key 按 §17.2 的规范拼，`evidence.json` 能读回来。

    时间戳用紧凑写法而不是 ISO 8601：`validate_key()` 只放行
    `[A-Za-z0-9._/-]`，ISO 里的冒号会被当场拒收。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    result, _ = run_pipeline(make_request(golden, tmp_path / "scratch"), HEALTHY, store=store)

    assert result.state is TaskValidationState.VALID
    keys = sorted(result.artifacts)
    prefix = f"tasks/{TASK_ID}/validation/"
    assert all(k.startswith(prefix) for k in keys)
    names = {k.rsplit("/", 1)[1] for k in keys}
    assert {"s4-baseline.junit.xml", "s7-gold.junit.xml", "s8-rerun-2.junit.xml"} <= names
    assert ":" not in result.artifacts[keys[0]].key

    assert result.evidence_uri is not None
    evidence_key = result.evidence_uri.removeprefix("local://").removesuffix(".gz")
    restored = json.loads(store.get(evidence_key))
    assert restored["task_id"] == TASK_ID
    assert restored["state"] == TaskValidationState.VALID.value
    assert [s["step"] for s in restored["steps"]] == [
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
        "S6",
        "S7",
        "S8",
    ]


def test_no_store_still_returns_a_verdict(golden: Any, tmp_path: Path) -> None:
    """不给制品存储也要能验（单测用法），只是拿不到证据引用。"""
    result, _ = run_pipeline(make_request(golden, tmp_path), HEALTHY)

    assert result.state is TaskValidationState.VALID
    assert result.evidence_uri is None
    assert not result.artifacts
