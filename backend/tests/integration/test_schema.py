"""数据库层面的协议约束检查。

上一层（`tests/unit/test_enum_consistency.py`）检查的是"代码里的枚举和协议一致"。
这一层检查的是"数据库真的会拒绝违反协议的记录"。两层缺一不可 ——
枚举写对了，也可能忘了给表加约束，那样错误数据照样进得来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.enums import (
    ALL_DB_ENUMS,
    AgentKind,
    AgentOutcome,
    BenchmarkSetStatus,
    ImageBuildStatus,
    InfraOutcome,
    IssueLanguage,
    LifecycleStatus,
    TaskDifficulty,
    TaskValidationState,
)
from app.domain.protocol import LEGAL_COMBINATIONS, is_legal_combination
from app.infrastructure.models import (
    Agent,
    AgentConfig,
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    EvaluationRun,
    EvaluationTaskRun,
    Repository,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def fixture_ids(session: Session) -> dict[str, int]:
    """建一套最小的关联数据：仓库 → 环境 → 题目 → 数据集 → Agent 配置 → 实验。

    评测执行记录挂着 6 个外键，不先把这条链建起来就没法测它身上的约束。
    """
    repo = Repository(full_name="acme/demo", url="https://example.com/acme/demo", language="Python")
    session.add(repo)
    session.flush()

    env = EnvironmentSpec(
        environment_id="demo__py311__v1",
        repository_id=repo.id,
        python_version="3.11",
        install_command="pip install -e .",
        test_command="pytest",
        test_report_path="report.xml",
        build_status=ImageBuildStatus.READY,
    )
    session.add(env)
    session.flush()

    task = BenchmarkTask(
        task_id="acme__demo-1",
        repository_id=repo.id,
        environment_spec_id=env.id,
        base_commit="a" * 40,
        issue_title="重连之后消息丢失",
        issue_body="断线重连后第一条消息不会被处理。",
        issue_language=IssueLanguage.ZH,
        fail_to_pass=["tests/test_adapter.py::test_reconnect"],
        pass_to_pass=["tests/test_adapter.py::test_connect"],
        test_patch_uri="tasks/acme__demo-1/test_patch.diff",
        test_patch_paths=["tests/test_adapter.py"],
        gold_patch_uri="tasks/acme__demo-1/gold_patch.diff",
        difficulty=TaskDifficulty.MEDIUM,
        validation_state=TaskValidationState.VALID,
        content_hash="b" * 64,
        raw_definition={},
    )
    second_task = BenchmarkTask(
        task_id="acme__demo-2",
        repository_id=repo.id,
        environment_spec_id=env.id,
        base_commit="c" * 40,
        issue_title="另一道题",
        issue_body="占位。",
        issue_language=IssueLanguage.EN,
        fail_to_pass=["tests/test_x.py::test_y"],
        pass_to_pass=[],
        test_patch_uri="tasks/acme__demo-2/test_patch.diff",
        test_patch_paths=["tests/test_x.py"],
        gold_patch_uri="tasks/acme__demo-2/gold_patch.diff",
        difficulty=TaskDifficulty.EASY,
        validation_state=TaskValidationState.VALID,
        content_hash="d" * 64,
        raw_definition={},
    )
    session.add_all([task, second_task])

    bench_set = BenchmarkSet(
        slug="demo", version="v1", title="演示集", status=BenchmarkSetStatus.PUBLISHED
    )
    agent = Agent(
        name="oracle",
        display_name="Oracle 哨兵",
        kind=AgentKind.ORACLE,
        adapter_class="app.runner.adapters.oracle.OracleRunner",
    )
    session.add_all([bench_set, agent])
    session.flush()

    config = AgentConfig(
        agent_id=agent.id,
        label="oracle@gold",
        agent_version="1",
        model_name="-",
        config_hash="e" * 64,
    )
    session.add(config)
    session.flush()

    run = EvaluationRun(
        name="demo run", benchmark_set_id=bench_set.id, agent_config_id=config.id, total_tasks=2
    )
    session.add(run)
    session.flush()

    return {"run_id": run.id, "task_id": task.id, "second_task_id": second_task.id}


def _task_run(fixture_ids: dict[str, int], **overrides: Any) -> EvaluationTaskRun:
    payload: dict[str, Any] = {
        "evaluation_run_id": fixture_ids["run_id"],
        "benchmark_task_id": fixture_ids["task_id"],
        "attempt_no": 1,
        "lifecycle_status": LifecycleStatus.QUEUED,
    }
    payload.update(overrides)
    return EvaluationTaskRun(**payload)


# ── 枚举 ────────────────────────────────────────────────────────


def test_task_resource_limits_are_real_columns(
    session: Session, fixture_ids: dict[str, int]
) -> None:
    """三个沙箱限额都要是独立列，能直接查。

    `sandbox_pids_limit` 是 2026-09-04 补的（issue #60）：任务 Schema §7.1 里一直有它，
    但 0001 建表时漏了，只有另外两个有列。

    为什么不塞进 `raw_definition` JSONB：三个都是起容器时要读的限额，存法不一致的话
    E2-T2 得为其中一个写特例，取不到还要兜默认值 —— 又多一处"静默用错默认值"的地方。
    pids 上限挡的是 fork 炸弹，兜错了防线就没了。
    """
    task = session.get(BenchmarkTask, fixture_ids["task_id"])
    assert task is not None
    assert (task.sandbox_cpu, task.sandbox_memory_mb, task.sandbox_pids_limit) == (
        Decimal("2.00"),
        2048,
        512,
    )

    # 能进 WHERE 子句才算"真的是列"，JSONB 里的字段做不到这么直接
    found = session.execute(
        text("SELECT task_id FROM benchmark_tasks WHERE sandbox_pids_limit = 512")
    ).scalars()
    assert "acme__demo-1" in set(found)


def test_database_enum_values_match_code(engine: Engine) -> None:
    """数据库里建出来的枚举类型，取值和顺序都要和代码一致（协议 C-46、C-47）。

    顺序也查：原生枚举在 PostgreSQL 里是有序类型，`ORDER BY status` 按的是
    定义顺序而不是字母序。顺序变了，排序结果会静默变化。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.typname, e.enumlabel "
                "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE t.typtype = 'e' ORDER BY t.typname, e.enumsortorder"
            )
        ).all()

    in_db: dict[str, list[str]] = {}
    for type_name, label in rows:
        in_db.setdefault(type_name, []).append(label)

    expected = {name: [m.value for m in cls] for name, cls in ALL_DB_ENUMS.items()}
    assert in_db == expected


# ── 三字段组合（协议 C-68、C-78）────────────────────────────────


@pytest.mark.parametrize(
    "combo",
    LEGAL_COMBINATIONS,
    ids=lambda c: f"{c.lifecycle_status}-{c.infra_outcome}-{c.agent_outcome}",
)
def test_legal_combination_is_accepted(
    session: Session, fixture_ids: dict[str, int], combo: Any
) -> None:
    """协议 §4.3 列出的 19 种合法终态组合，每一种都必须能写进去。

    反过来说，这条测试也保证了 CHECK 约束没有写得过严 —— 过严比过松更难发现，
    因为它要等到真的跑出那种情况才暴露，而那时实验已经跑了几个小时。
    """
    session.add(
        _task_run(
            fixture_ids,
            lifecycle_status=combo.lifecycle_status,
            infra_outcome=combo.infra_outcome,
            agent_outcome=combo.agent_outcome,
            agent_started_at=NOW if combo.agent_started is not False else None,
        )
    )
    session.flush()


def test_illegal_combinations_are_rejected(session: Session, fixture_ids: dict[str, int]) -> None:
    """表外的组合一律写不进去（协议 C-78：禁止静默落库）。

    这里挑了 4 组最有代表性的：

    - 平台故障却判 AI 修好了 —— 最危险的一种，会直接抬高解决率
    - 平台故障却判空补丁 —— 协议 C-30 明令禁止（对应断言 T-20）
    - 没启动过 AI 却给出了结论 —— 违反 C-69
    - OOM 判成"从未启动" —— 这正是穷举检查抓到的第 8 组矛盾
    """
    cases = [
        (LifecycleStatus.FAILED, InfraOutcome.SANDBOX_ERROR, AgentOutcome.RESOLVED, NOW),
        (LifecycleStatus.FAILED, InfraOutcome.OOM_KILLED, AgentOutcome.EMPTY_PATCH, NOW),
        (LifecycleStatus.COMPLETED, InfraOutcome.SUCCESS, AgentOutcome.RESOLVED, None),
        (LifecycleStatus.FAILED, InfraOutcome.OOM_KILLED, AgentOutcome.NOT_ATTEMPTED, None),
    ]
    for lifecycle, infra, agent_outcome, started in cases:
        assert not is_legal_combination(
            lifecycle, infra, agent_outcome, agent_started=started is not None
        ), "这条用例本身写错了"
        savepoint = session.begin_nested()
        session.add(
            _task_run(
                fixture_ids,
                lifecycle_status=lifecycle,
                infra_outcome=infra,
                agent_outcome=agent_outcome,
                agent_started_at=started,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        savepoint.rollback()


def test_non_terminal_cannot_have_agent_outcome(
    session: Session, fixture_ids: dict[str, int]
) -> None:
    """非终态时 agent_outcome 必须为空（协议 C-09、C-29）。"""
    session.add(
        _task_run(
            fixture_ids,
            lifecycle_status=LifecycleStatus.AGENT_RUNNING,
            agent_outcome=AgentOutcome.RESOLVED,
            agent_started_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_check_constraint_covers_every_enumerated_combination(
    session: Session, fixture_ids: dict[str, int]
) -> None:
    """拿一部分穷举组合过一遍数据库，确认 CHECK 的判断和代码里的判断完全一致。

    只挑终态 × 全部 infra × 全部 agent_outcome（3 × 13 × 6 = 234 种），
    非终态那 91 种由上一条测试代表。全跑 780 种要几十秒，不值得。
    """
    terminal = [LifecycleStatus.COMPLETED, LifecycleStatus.FAILED, LifecycleStatus.CANCELLED]
    agent_values: list[AgentOutcome | None] = [*AgentOutcome, None]

    mismatches: list[str] = []
    for lifecycle, infra, agent_outcome in product(terminal, InfraOutcome, agent_values):
        expected_ok = is_legal_combination(lifecycle, infra, agent_outcome)
        # agent_started_at 按代码里那条规则给，让"组合本身合不合法"成为唯一变量
        started = None
        for combo in LEGAL_COMBINATIONS:
            if (combo.lifecycle_status, combo.infra_outcome, combo.agent_outcome) == (
                lifecycle,
                infra,
                agent_outcome,
            ):
                started = NOW if combo.agent_started is not False else None
                break

        savepoint = session.begin_nested()
        session.add(
            _task_run(
                fixture_ids,
                lifecycle_status=lifecycle,
                infra_outcome=infra,
                agent_outcome=agent_outcome,
                agent_started_at=started,
            )
        )
        try:
            session.flush()
            actual_ok = True
        except IntegrityError:
            actual_ok = False
        savepoint.rollback()

        if actual_ok != expected_ok:
            mismatches.append(
                f"{lifecycle}+{infra}+{agent_outcome}：代码说 {expected_ok}，数据库说 {actual_ok}"
            )

    assert not mismatches, "代码里的合法组合判断和数据库 CHECK 约束不一致：\n" + "\n".join(
        mismatches
    )


# ── 唯一性（协议 C-48、C-57）────────────────────────────────────


def test_attempt_number_is_unique_per_task(session: Session, fixture_ids: dict[str, int]) -> None:
    """同一次实验的同一道题，attempt 编号不能重复（协议 C-48）。"""
    session.add(_task_run(fixture_ids, attempt_no=1))
    session.flush()
    session.add(_task_run(fixture_ids, attempt_no=1))
    with pytest.raises(IntegrityError):
        session.flush()


def test_only_one_canonical_attempt_per_task(session: Session, fixture_ids: dict[str, int]) -> None:
    """每道题至多一个认定结果（协议 C-57）。

    这条约束是解决率算得对不对的底线。少了它，一道题重试 3 次就可能被统计 3 次。
    """
    session.add(_task_run(fixture_ids, attempt_no=1, is_canonical=True))
    session.flush()

    # 同一道题的第二个认定结果 → 撞部分唯一索引
    session.add(_task_run(fixture_ids, attempt_no=2, is_canonical=True))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_multiple_non_canonical_attempts_are_allowed(
    session: Session, fixture_ids: dict[str, int]
) -> None:
    """同一道题可以有多次重试，只是其中至多一次被标为认定结果。

    这条和上一条是一对：约束必须挡住"两个认定结果"，但不能挡住"多次重试"。
    """
    session.add_all(
        [
            _task_run(fixture_ids, attempt_no=1, is_canonical=False),
            _task_run(fixture_ids, attempt_no=2, is_canonical=False),
            _task_run(fixture_ids, attempt_no=3, is_canonical=True),
        ]
    )
    session.flush()


def test_canonical_is_scoped_to_one_task(session: Session, fixture_ids: dict[str, int]) -> None:
    """认定结果的唯一性是按题算的，不是按整次实验算的。"""
    session.add_all(
        [
            _task_run(fixture_ids, is_canonical=True),
            _task_run(
                fixture_ids,
                benchmark_task_id=fixture_ids["second_task_id"],
                is_canonical=True,
            ),
        ]
    )
    session.flush()


def test_attempt_number_starts_at_one(session: Session, fixture_ids: dict[str, int]) -> None:
    """attempt 编号从 1 开始，0 和负数写不进去。"""
    session.add(_task_run(fixture_ids, attempt_no=0))
    with pytest.raises(IntegrityError):
        session.flush()


# ── 其他协议要求 ────────────────────────────────────────────────


def test_protocol_version_is_written_by_default(
    session: Session, fixture_ids: dict[str, int]
) -> None:
    """实验创建时必须带上协议版本号（协议 C-67）。"""
    run = session.get(EvaluationRun, fixture_ids["run_id"])
    assert run is not None
    assert run.protocol_version == "v1.2"
    assert run.dirty is False
    assert run.total_cost_usd == Decimal("0")
