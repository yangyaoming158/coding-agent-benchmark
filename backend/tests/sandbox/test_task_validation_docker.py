"""题目验证流水线的端到端验收（E1-T3 的 AC，需要 Docker）。

**这一组真起容器、真跑 pytest**，`make test` 会跳过，`make test-docker` 才跑。
它就是 E1-T3 验收标准的证据：

    对 Golden Task 全部判 VALID；对人为构造的坏任务全部判对应 INVALID reason_code

## 和 `test_task_validation.py` 的分工

那一组用假容器验八步的**判定逻辑**，毫秒级、每次提交都跑。
这一组验的是另一件事：这套逻辑接上真 pytest、真 junit 报告、真容器之后仍然成立。
两层都要有 —— 假的证明不了链路通，真的又太慢，做不到每次提交都跑。

## 坏任务是"改出来"的，不是手写的

七种坏任务都由真实的 Golden 题现改：改 `base_commit`、改镜像名、改超时预算，
或者把 `gold_patch` 换成一段现生成的补丁。补丁用"物化工作区 → 改文件 → git diff"
的办法造，和真实评测里补丁的来路一致 —— 手写 diff 上下文行数一错就打不上，
而那种失败看起来像流水线有 bug。

`gold_patch` 的两个变体直接从 Golden 题的源码目录派生（改一行、加一行注释），
所以源码一改，这里的 `assert` 会立刻红，不会悄悄失效。
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.benchmark.schema import TaskDefinition
from app.domain.enums import TaskInvalidReason, TaskValidationState
from app.evaluation.executor import DEFAULT_GOLDEN_IMAGE
from app.evaluation.validation import ValidationRequest, ValidationResult, validate_task
from app.sandbox.container import DockerUnavailableError, get_docker_client
from app.sandbox.git_cli import run_git
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import materialize_workspace
from app.storage.local import LocalArtifactStore
from cli.golden import SOURCES_DIR, build

pytestmark = pytest.mark.docker

TEXTKIT = "bench-golden__textkit-1"
SOURCE_FILE = "textkit/csvline.py"

#: 造"修不好"的补丁时，往源码里加的那一行。加注释不改行为，
#: 所以补丁能干净打上，但 F2P 照样挂 —— 这正是 GOLD_NOT_FIXING 的样子。
NOOP_COMMENT = "\n# 这行注释什么都没修\n"

#: 造"引入回归"的补丁时，把官方修复里的这一句换掉。
#: 去掉 rstrip 之后：三条 F2P（引号里的逗号）照样全过，
#: 但 `test_strips_trailing_newline` 这条 P2P 会挂 —— 正是 GOLD_REGRESSION。
RSTRIP_LINE = 'text = line.rstrip("\\n")'
RSTRIP_REPLACEMENT = "text = line"


# ══════════════════════════════════════════════════════════════
# 固定装置
# ══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def image() -> str:
    """确认测试镜像在本机。不在就跳过，并说清楚怎么建。"""
    try:
        client = get_docker_client()
    except DockerUnavailableError as exc:
        pytest.skip(f"Docker 不可用：{exc}")
    try:
        client.images.get(DEFAULT_GOLDEN_IMAGE)
    except Exception:
        pytest.skip(f"没有 {DEFAULT_GOLDEN_IMAGE}，先跑 `make images`")
    return DEFAULT_GOLDEN_IMAGE


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, TaskDefinition], Path]:
    """把四道 Golden 题连同镜像仓库现建一份。

    现建而不是用 `var/mirrors/`：那个目录在 .gitignore 里，换台机器就是空的。
    """
    mirror_root = tmp_path_factory.mktemp("mirrors")
    output = build([], mirror_root=mirror_root)
    tasks = {
        task_id: TaskDefinition.model_validate_json(text) for task_id, text in output.tasks.items()
    }
    return tasks, mirror_root


def make_request(
    task: TaskDefinition, mirror_root: Path, scratch: Path, image: str, **overrides: Any
) -> ValidationRequest:
    request = ValidationRequest(
        plan=task.execution_plan(),
        gold_patch=task.gold_patch,
        repo_name=task.repo_name,
        mirror_root=mirror_root,
        scratch_dir=scratch,
        image=image,
        repo_url=task.repo_url,
        content_hash=task.content_hash,
        # 坏任务只要看落在哪个 reason code 上，不用复跑找不稳定用例。
        # 想验复跑的用例自己覆盖回 2
        repeat=1,
    )
    return replace(request, **overrides) if overrides else request


def patched_task(task: TaskDefinition, **fields: Any) -> TaskDefinition:
    """改几个字段造一道坏题。

    `content_hash` 置空让模型重算 —— 不置空的话，模型会拿声明的哈希和重算结果
    比对，当场以"content_hash 对不上"拒收，我们就走不到流水线那一步。
    """
    payload = json.loads(task.model_dump_json())
    payload.update(fields)
    payload["content_hash"] = None
    return TaskDefinition.model_validate(payload)


def diff_after_editing(task: TaskDefinition, mirror_root: Path, edits: Mapping[str, str]) -> str:
    """物化一份 base 工作区、按 `edits` 改文件、`git diff` 出补丁。

    和真实评测里补丁的来路一致（E3-T3 的 `capture_workspace_diff`）。
    """
    mirror = MirrorManager(mirror_root).path_for(task.repo_name)
    with tempfile.TemporaryDirectory(prefix="bad-task-") as tmp:
        workspace = materialize_workspace(
            mirror_path=mirror, base_commit=task.base_commit, dest=Path(tmp) / "ws"
        )
        for relative, content in edits.items():
            (workspace.path / relative).write_text(content, encoding="utf-8")
        return run_git(["diff"], cwd=workspace.path).stdout


def golden_source(task_id: str, side: str) -> str:
    """读 Golden 题的源码。`side` 是 `base` 或 `fix`。"""
    return (SOURCES_DIR / task_id / side / SOURCE_FILE).read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# AC 上半：四道 Golden 题全部判 VALID
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "task_id",
    [
        "bench-golden__textkit-1",
        "bench-golden__auth-2",
        "bench-golden__cart-3",
        "bench-golden__pager-4",
    ],
)
def test_golden_task_is_valid(task_id: str, golden: Any, image: str, tmp_path: Path) -> None:
    """八步全过 → `VALID`，而且证据齐全。

    这一条同时是协议 C-50 的逐题证据：S5 证明每条 F2P 在修复前都挂
    （Noop 解决率 0% 的依据），S7 证明打完官方补丁全过（Oracle 100% 的依据）。
    """
    tasks, mirror_root = golden
    store = LocalArtifactStore(tmp_path / "artifacts")
    result = validate_task(
        make_request(tasks[task_id], mirror_root, tmp_path / "ws", image, repeat=2),
        store=store,
    )

    assert result.state is TaskValidationState.VALID, result.steps[-1].detail
    assert result.reason_code is None
    assert [s.step for s in result.steps] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]

    # §7.3 最后一行要求 VALID 时写下镜像、用例清单、耗时基线
    assert result.evidence["image"]["image_id"].startswith("sha256:")
    assert result.evidence["baseline"]["total_cases"] >= len(tasks[task_id].fail_to_pass)
    assert result.evidence["timing_baseline_ms"] > 0
    assert result.evidence_uri is not None

    # 逐条对照：F2P 在基线上全挂、打完 gold 全过
    baseline_f2p = result.evidence["baseline"]["fail_to_pass"]
    assert set(baseline_f2p.values()) <= {"FAILED", "ERROR"}
    assert set(result.evidence["after_gold"]["fail_to_pass"].values()) == {"PASSED"}
    assert set(result.evidence["after_gold"]["pass_to_pass"].values()) == {"PASSED"}


def test_evidence_is_readable_after_the_run(golden: Any, image: str, tmp_path: Path) -> None:
    """证据落盘之后要能原样读回来 —— §7.3 说的"任务本身也要可审计"。"""
    tasks, mirror_root = golden
    store = LocalArtifactStore(tmp_path / "artifacts")
    result = validate_task(
        make_request(tasks[TEXTKIT], mirror_root, tmp_path / "ws", image), store=store
    )

    assert result.state is TaskValidationState.VALID
    names = {key.rsplit("/", 1)[1] for key in result.artifacts}
    assert {"s4-baseline.junit.xml", "s4-baseline.stdout.log", "s7-gold.junit.xml"} <= names

    junit_key = next(k for k in result.artifacts if k.endswith("s4-baseline.junit.xml"))
    assert b"<testsuite" in store.get(junit_key)

    evidence_key = next(k for k in result.artifacts if k.endswith("evidence.json"))
    restored = json.loads(store.get(evidence_key))
    assert restored["task_id"] == TEXTKIT
    assert restored["state"] == "VALID"


# ══════════════════════════════════════════════════════════════
# AC 下半：七种坏任务各自落到对应的 reason code
# ══════════════════════════════════════════════════════════════


def test_repo_unavailable(golden: Any, image: str, tmp_path: Path) -> None:
    """S1：镜像仓库不在本地。"""
    tasks, _ = golden
    result = validate_task(
        make_request(tasks[TEXTKIT], tmp_path / "no-mirrors", tmp_path / "ws", image)
    )
    assert_invalid(result, TaskInvalidReason.REPO_UNAVAILABLE)


def test_commit_missing(golden: Any, image: str, tmp_path: Path) -> None:
    """S2：`base_commit` 是 40 位合法 SHA，但镜像里没有这个对象。"""
    tasks, mirror_root = golden
    bad = patched_task(tasks[TEXTKIT], base_commit="0" * 40)
    result = validate_task(make_request(bad, mirror_root, tmp_path / "ws", image))
    assert_invalid(result, TaskInvalidReason.COMMIT_MISSING)


def test_env_unbuildable(golden: Any, image: str, tmp_path: Path) -> None:
    """S3：环境镜像不在本地。E2-T3 之前这里只做 reuse 那一半。"""
    tasks, mirror_root = golden
    result = validate_task(
        make_request(tasks[TEXTKIT], mirror_root, tmp_path / "ws", "bench-nonexistent:v0")
    )
    assert_invalid(result, TaskInvalidReason.ENV_UNBUILDABLE)


def test_test_too_slow(golden: Any, image: str, tmp_path: Path) -> None:
    """S4：全量套件跑不完预算。

    把 `test_command` 换成一句"睡 30 秒"，预算给 5 秒。这样这条用例的结果只取决于
    这两个数，**不取决于机器有多快** —— 一开始写的是"预算压到 1 秒"，结果本机
    容器起来加跑完七条用例还不到 1 秒，用例就绿不了了（2026-09-07 实测）。
    """
    tasks, mirror_root = golden
    request = make_request(tasks[TEXTKIT], mirror_root, tmp_path / "ws", image)
    request = replace(
        request,
        plan=replace(
            request.plan,
            test_command='python -c "import time; time.sleep(30)"',
            test_timeout_s=5,
        ),
    )
    assert_invalid(validate_task(request), TaskInvalidReason.TEST_TOO_SLOW)


def test_f2p_not_failing(golden: Any, image: str, tmp_path: Path) -> None:
    """S5：把一条本来就通过的用例塞进 `fail_to_pass`。

    这正是"Noop 哨兵会给出非零解决率"的那种坏题。
    """
    tasks, mirror_root = golden
    task = tasks[TEXTKIT]
    already_passing = task.pass_to_pass[0]
    # F2P 和 P2P 不能有交集，所以要把它从 P2P 里拿掉；
    # strategy=full 时候选池大小必须等于 P2P 条数，也得跟着改（§7.7）
    remaining = [t for t in task.pass_to_pass if t != already_passing]
    bad = patched_task(
        task,
        fail_to_pass=[already_passing],
        pass_to_pass=remaining,
        p2p_sampling={"strategy": "full", "seed": None, "total_pool": len(remaining)},
    )
    result = validate_task(make_request(bad, mirror_root, tmp_path / "ws", image))
    assert_invalid(result, TaskInvalidReason.F2P_NOT_FAILING)
    assert already_passing in result.steps[-1].detail


def test_gold_not_fixing(golden: Any, image: str, tmp_path: Path) -> None:
    """S7：`gold_patch` 能打上，但什么都没修好。"""
    tasks, mirror_root = golden
    task = tasks[TEXTKIT]
    noop_patch = diff_after_editing(
        task, mirror_root, {SOURCE_FILE: golden_source(TEXTKIT, "base") + NOOP_COMMENT}
    )
    assert noop_patch.strip(), "补丁不该是空的，否则模型会当场拒收这道题"

    bad = patched_task(task, gold_patch=noop_patch)
    result = validate_task(make_request(bad, mirror_root, tmp_path / "ws", image))
    assert_invalid(result, TaskInvalidReason.GOLD_NOT_FIXING)


def test_gold_regression(golden: Any, image: str, tmp_path: Path) -> None:
    """S8：`gold_patch` 修好了 F2P，却把一条 P2P 打挂了。

    做法是拿官方修复去掉 `rstrip("\\n")`：三条 F2P（引号里的逗号）照样全过，
    但 `test_strips_trailing_newline` 会挂。
    """
    tasks, mirror_root = golden
    task = tasks[TEXTKIT]
    fixed = golden_source(TEXTKIT, "fix")
    assert RSTRIP_LINE in fixed, (
        f"{SOURCE_FILE} 的官方修复里找不到 {RSTRIP_LINE!r}，这条用例要跟着改"
    )
    regressed = fixed.replace(RSTRIP_LINE, RSTRIP_REPLACEMENT, 1)

    bad = patched_task(
        task, gold_patch=diff_after_editing(task, mirror_root, {SOURCE_FILE: regressed})
    )
    result = validate_task(make_request(bad, mirror_root, tmp_path / "ws", image))
    assert_invalid(result, TaskInvalidReason.GOLD_REGRESSION)
    assert "test_strips_trailing_newline" in result.steps[-1].detail


def assert_invalid(result: ValidationResult, reason: TaskInvalidReason) -> None:
    """坏任务必须判 `INVALID` + 指定的 reason code，而且不能是"没得出结论"。"""
    assert result.error is None, f"不该是平台故障：{result.error}"
    assert result.state is TaskValidationState.INVALID, result.state
    assert result.reason_code is reason, result.steps[-1].detail
