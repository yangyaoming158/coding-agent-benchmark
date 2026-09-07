"""验证结论怎么落库（E1-T3）。

要一个真的 PostgreSQL，不要 Docker 里的测试镜像 —— 这里验的是"拿到结论之后
往库里写什么"，结论本身直接构造，不跑流水线。跑流水线的那两层在
`tests/sandbox/test_task_validation{,_docker}.py`。

三件事必须验到：

1. 结论、reason code、证据 uri 都写进 `benchmark_tasks`，`show` 查得到；
2. 镜像 digest 回填进 `environment_specs`（协议 C-36 要求引用 digest 不引用 tag）；
3. **平台故障时不动题目状态** —— 这条最容易写错，也最容易造成长期误导。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import (
    ArtifactKind,
    ArtifactOwnerType,
    TaskInvalidReason,
    TaskValidationState,
)
from app.evaluation.validation import ValidationResult
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.benchmark import BenchmarkTask, EnvironmentSpec
from app.sandbox.container import ImageInfo
from app.storage.base import ArtifactBackend, ArtifactRef
from cli.validate import _load, _Loaded, _mark_validating, _write_back
from tests.integration.factories import seed_minimal, wipe

pytestmark = pytest.mark.db

EVIDENCE_KEY = "tasks/bench-golden__textkit-1/validation/20260907T120000Z/evidence.json"
IMAGE_DIGEST = "sha256:" + "ab" * 32


def make_result(
    state: TaskValidationState | None,
    *,
    reason: TaskInvalidReason | None = None,
    error: str | None = None,
    with_artifact: bool = True,
) -> ValidationResult:
    ref = ArtifactRef(
        key=EVIDENCE_KEY,
        uri=f"local://{EVIDENCE_KEY}.gz",
        backend=ArtifactBackend.LOCAL,
        content_type="application/json",
        size_bytes=42,
        stored_bytes=30,
        sha256="c" * 64,
        compressed=True,
    )
    return ValidationResult(
        state=state,
        reason_code=reason,
        steps=(),
        evidence={"task_id": "bench-golden__textkit-1"},
        artifacts={EVIDENCE_KEY: ref} if with_artifact else {},
        evidence_uri=ref.uri if with_artifact else None,
        error=error,
        validated_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        image=ImageInfo(tag="bench-golden:py311", image_id="sha256:x", digest=IMAGE_DIGEST),
    )


@pytest.fixture
def seeded(session: Session) -> int:
    """一道题，状态停在 `DISCOVERED`（`make seed-tasks` 之后就是这样）。"""
    wipe(session)
    seeded = seed_minimal(session, tasks=1)
    session.commit()
    return seeded.task_ids[0]


def read_task(session: Session, row_id: int) -> BenchmarkTask:
    return session.get(BenchmarkTask, row_id)  # type: ignore[return-value]


def test_valid_result_lands_in_the_row(session: Session, seeded: int) -> None:
    _write_back(session, _loaded(session, seeded), make_result(TaskValidationState.VALID))
    session.flush()

    row = read_task(session, seeded)
    assert row.validation_state is TaskValidationState.VALID
    assert row.invalid_reason_code is None
    assert row.validated_at is not None
    assert row.validation_evidence_uri == f"local://{EVIDENCE_KEY}.gz"


def test_invalid_result_records_the_reason_code(session: Session, seeded: int) -> None:
    result = make_result(TaskValidationState.INVALID, reason=TaskInvalidReason.F2P_NOT_FAILING)
    _write_back(session, _loaded(session, seeded), result)
    session.flush()

    row = read_task(session, seeded)
    assert row.validation_state is TaskValidationState.INVALID
    # 列是 varchar，存的是枚举的字面值 —— 取值的正确性靠 Python 侧的枚举保证
    assert row.invalid_reason_code == "F2P_NOT_FAILING"


def test_platform_fault_leaves_the_state_alone(session: Session, seeded: int) -> None:
    """没得出结论时状态要恢复原样，**不能**写 INVALID。

    把平台故障记成题目无效，下次就再没人会去查真正的原因了 ——
    这和协议里"平台故障不算进解决率"是同一个道理。
    """
    loaded = _loaded(session, seeded)
    _mark_validating(session, seeded)
    session.flush()
    assert read_task(session, seeded).validation_state is TaskValidationState.VALIDATING

    _write_back(session, loaded, make_result(None, error="连不上 docker"))
    session.flush()

    row = read_task(session, seeded)
    assert row.validation_state is TaskValidationState.DISCOVERED
    assert row.invalid_reason_code is None
    assert row.validation_evidence_uri is None


def test_image_digest_is_backfilled(session: Session, seeded: int) -> None:
    """协议 C-36：引用镜像要用 digest 不用 tag。验证时拿到了就顺手记下来。"""
    loaded = _loaded(session, seeded)
    _write_back(session, loaded, make_result(TaskValidationState.VALID))
    session.flush()

    env = session.get(EnvironmentSpec, loaded.environment_spec_id)
    assert env is not None and env.image_digest == IMAGE_DIGEST


def test_evidence_gets_an_artifact_row(session: Session, seeded: int) -> None:
    """证据制品要在 `artifacts` 表里留索引行，否则
    `validation_evidence_uri` 会指向一个库里查不到的东西。"""
    _write_back(session, _loaded(session, seeded), make_result(TaskValidationState.VALID))
    session.flush()

    rows = list(session.execute(sa.select(Artifact)).scalars())
    assert len(rows) == 1
    assert rows[0].owner_type is ArtifactOwnerType.VALIDATION
    assert rows[0].owner_id == seeded
    assert rows[0].kind is ArtifactKind.VALIDATION_EVIDENCE


def test_load_reads_the_full_definition_from_raw_definition(session: Session, seeded: int) -> None:
    """题目从 `raw_definition` 还原。补丁正文只在那里，不在那十几个列里。"""
    golden = Path(__file__).resolve().parents[3] / "datasets" / "golden"
    payload = json.loads((golden / "bench-golden__textkit-1.json").read_text(encoding="utf-8"))
    session.execute(
        sa.update(BenchmarkTask).where(BenchmarkTask.id == seeded).values(raw_definition=payload)
    )
    session.flush()

    loaded = _load(session, [])
    assert len(loaded) == 1
    assert loaded[0].task.gold_patch.startswith("diff --git")
    assert loaded[0].previous_state is TaskValidationState.DISCOVERED
    # 环境规格还没有 image_tag（E2-T3 之前都是这样），退回 Golden 的手写镜像
    assert loaded[0].image == "bench-golden:py311"


def _loaded(session: Session, row_id: int) -> _Loaded:
    """按 `_load` 的口径取一条，避免测试里自己拼 `_Loaded`。"""
    row = read_task(session, row_id)
    return _Loaded(
        row_id=row.id,
        task=None,  # type: ignore[arg-type]  写回不看题目定义，只看结论
        image="bench-golden:py311",
        extra_protected_paths=(),
        environment_spec_id=row.environment_spec_id,
        previous_state=row.validation_state,
    )
