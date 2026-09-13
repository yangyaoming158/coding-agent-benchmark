"""实验接口（E7-T0）：建、看、取消、补跑，以及鉴权和错误形状。

这个文件里钉住的几件事：

- 写操作必须带 `X-Bench-Token`（AC-3）；
- 建实验走的是 `create_runs()`，不是路由自己拼行（AC-2）；
- 404 / 403 / 409 各有一条，错误体是 `{code, message}`（AC-8）；
- 运行列表和任务网格都是**一条 SQL**（AC-7）。
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.enums import EvaluationRunStatus, JobState, JobType, LifecycleStatus
from app.evaluation.manifest import ProvenanceError, collect_provenance
from app.evaluation.orchestrator import create_runs
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.infrastructure.models.job import JobQueue
from tests.integration.conftest import count_queries
from tests.integration.factories import provenance_for, seed_minimal, wipe

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


@pytest.fixture
def stub_provenance(client: TestClient, session: Session) -> Any:
    """把取可复现性事实那一步换成不调 git 的版本。

    **不是为了绕过协议 C-27。** `collect_provenance()` 会跑 `git status`，
    而开发时工作区永远是脏的 —— 不换掉的话，每个建实验的测试都会因为
    "工作区不干净"变红，而那和被测的东西（路由有没有把参数正确转给编排层）
    一点关系都没有。同一条分界线写在 `app.evaluation.manifest` 的模块文档里。

    生产路径没被改：下面 `test_create_run_uses_collect_provenance_by_default`
    钉住默认拿到的就是 `collect_provenance` 本人。
    """
    from app.api.deps import get_provenance_collector

    seeded = seed_minimal(session, tasks=3)
    session.commit()

    def fake(_session: Session, **kwargs: Any) -> Any:
        return provenance_for(seeded, task_ids=kwargs["task_ids"])

    client.app.dependency_overrides[get_provenance_collector] = lambda: fake  # type: ignore[attr-defined]
    return seeded


def _stage_snapshot(session: Session, seeded: Any) -> None:
    """把题冻进快照。`POST /api/runs` 从 `benchmark_set_items` 取题。"""
    from app.infrastructure.models.benchmark import BenchmarkSetItem

    for position, task_id in enumerate(seeded.task_ids):
        session.add(
            BenchmarkSetItem(
                benchmark_set_id=seeded.benchmark_set_id,
                benchmark_task_id=task_id,
                task_content_hash=f"{position:064d}",
                position=position,
            )
        )
    session.commit()


# ── 建实验 ──────────────────────────────────────────────────


def test_create_run_goes_through_the_orchestrator(
    client: TestClient, session: Session, admin_token: str, stub_provenance: Any
) -> None:
    """建实验落到 `create_runs()` 上：行上有 manifest，作业也投出去了。

    这两样东西是"没绕过编排层"的证据 —— 路由自己 INSERT 一行 `EvaluationRun`
    的话，manifest 会是空的，`job_queue` 里也不会有作业。
    """
    _stage_snapshot(session, stub_provenance)

    response = client.post(
        "/api/runs",
        json={
            "benchmark_set_id": stub_provenance.benchmark_set_id,
            "agent_config_id": stub_provenance.agent_config_id,
        },
        headers={"X-Bench-Token": admin_token},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["task_count"] == 3
    assert len(body["runs"]) == 1
    run_id = body["runs"][0]["id"]

    run = session.get(EvaluationRun, run_id)
    assert run is not None
    assert run.status is EvaluationRunStatus.QUEUED
    assert run.total_tasks == 3
    assert run.created_by == "api"
    # manifest 非空 = 走了 create_runs()（它的 provenance 是必填参数）
    assert run.manifest["harness_git_sha"]
    assert run.manifest["dataset"]["benchmark_set_id"] == stub_provenance.benchmark_set_id

    jobs = session.execute(
        sa.select(sa.func.count()).where(
            JobQueue.job_type == JobType.EVAL_TASK,
            JobQueue.payload["evaluation_run_id"].astext == str(run_id),
        )
    ).scalar_one()
    assert jobs == 3


def test_create_run_with_rounds_creates_that_many_experiments(
    client: TestClient, session: Session, admin_token: str, stub_provenance: Any
) -> None:
    """`rounds=3` 建的是 3 个独立实验（协议 C-55），不是一个实验跑 3 遍。"""
    _stage_snapshot(session, stub_provenance)

    response = client.post(
        "/api/runs",
        json={
            "benchmark_set_id": stub_provenance.benchmark_set_id,
            "agent_config_id": stub_provenance.agent_config_id,
            "rounds": 3,
        },
        headers={"X-Bench-Token": admin_token},
    )

    assert response.status_code == 201
    runs = response.json()["runs"]
    assert len(runs) == 3
    assert len({r["id"] for r in runs}) == 3


def test_create_run_uses_collect_provenance_by_default() -> None:
    """默认的凭证收集器就是 `collect_provenance` 本人。

    上面那个 stub 是测试专用的。这条钉住它**不会悄悄变成生产默认值** ——
    真变了的话，协议 C-27（脏工作区拒绝建实验）在 HTTP 这条路上就失效了，
    而且不会有任何报错。
    """
    from app.api.deps import get_provenance_collector

    assert get_provenance_collector() is collect_provenance


def test_create_run_rejects_a_dirty_workspace_with_409(
    client: TestClient, session: Session, admin_token: str
) -> None:
    """工作区不干净时建不了实验（协议 C-27），返回 409 不是 500。

    CLI 有 `--allow-dirty` 这个口子，HTTP 端点故意不给 —— 一个网页按钮能悄悄
    建出"不得进排行榜"的实验，过两天没人记得那次实验为什么带着 dirty 标记。
    """
    from app.api.deps import get_provenance_collector

    seeded = seed_minimal(session, tasks=1)
    _stage_snapshot(session, seeded)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ProvenanceError("工作区有未提交的改动，拒绝建实验（协议 C-27）")

    client.app.dependency_overrides[get_provenance_collector] = lambda: refuse  # type: ignore[attr-defined]

    response = client.post(
        "/api/runs",
        json={
            "benchmark_set_id": seeded.benchmark_set_id,
            "agent_config_id": seeded.agent_config_id,
        },
        headers={"X-Bench-Token": admin_token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_DIRTY"
    assert "C-27" in response.json()["message"]


def test_create_run_on_an_empty_snapshot_is_409(
    client: TestClient, session: Session, admin_token: str, stub_provenance: Any
) -> None:
    """快照里一道题都没有时是 409，不是建一个 0 题的实验。"""
    response = client.post(
        "/api/runs",
        json={
            "benchmark_set_id": stub_provenance.benchmark_set_id,
            "agent_config_id": stub_provenance.agent_config_id,
        },
        headers={"X-Bench-Token": admin_token},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "BENCHMARK_SET_EMPTY"


def test_create_run_with_unknown_agent_config_is_404(
    client: TestClient, session: Session, admin_token: str, stub_provenance: Any
) -> None:
    response = client.post(
        "/api/runs",
        json={"benchmark_set_id": stub_provenance.benchmark_set_id, "agent_config_id": 99999},
        headers={"X-Bench-Token": admin_token},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CONFIG_NOT_FOUND",
        "message": "找不到参赛者 #99999",
    }


# ── 鉴权（AC-3）─────────────────────────────────────────────


def test_write_without_token_is_403(client: TestClient, session: Session) -> None:
    """不带 token 的写操作一律 403，错误体也是 {code, message}。"""
    response = client.post("/api/runs", json={"benchmark_set_id": 1, "agent_config_id": 1})

    assert response.status_code == 403
    assert response.json() == {
        "code": "MISSING_TOKEN",
        "message": "写操作要带 X-Bench-Token 请求头",
    }


def test_write_with_wrong_token_is_403(
    client: TestClient, session: Session, admin_token: str
) -> None:
    response = client.post(
        "/api/runs",
        json={"benchmark_set_id": 1, "agent_config_id": 1},
        headers={"X-Bench-Token": admin_token + "-nope"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INVALID_TOKEN"


def test_reads_need_no_token(client: TestClient, session: Session) -> None:
    """读接口开放（§14.4）。"""
    assert client.get("/api/runs").status_code == 200


def test_app_refuses_to_start_without_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 `ADMIN_TOKEN` 时 `create_app()` 直接抛（AC-3）。

    为什么不是"没配就放行"：默认放行的部署从外面看和配好了的一模一样，
    没有任何症状，直到有人发现谁都能掐掉一场跑了两小时的实验。
    """
    from app.api.app import create_app
    from app.infrastructure.config import reset_settings_cache

    monkeypatch.setenv("ADMIN_TOKEN", "")
    reset_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
            create_app()
    finally:
        reset_settings_cache()


# ── 查询 ────────────────────────────────────────────────────


def test_run_list_query_count_does_not_grow_with_rows(
    client: TestClient, session: Session, engine: Engine
) -> None:
    """运行列表的 SQL 条数不随行数增长（AC-7）。

    一行里要显示数据集名和参赛者名，那两个是 join 出来的。一不留神就写成
    "先查运行，再逐行查名字"，20 个运行就是 41 条查询。

    断言的是**条数不变**，不是某个具体数字：具体数字会随实现细节漂
    （多一条 count(*)、少一条存在性检查都算正常），而"不随行数增长"
    才是 AC-7 真正要的性质。
    """
    seeded = seed_minimal(session, tasks=1)

    def add_runs(count: int) -> None:
        for _ in range(count):
            create_runs(
                session,
                name="n",
                task_ids=list(seeded.task_ids),
                provenance=provenance_for(seeded),
            )
        session.commit()

    add_runs(3)
    with count_queries(engine) as few:
        assert client.get("/api/runs").json()["total"] == 3

    add_runs(12)
    with count_queries(engine) as many:
        assert client.get("/api/runs").json()["total"] == 15

    assert few.count == many.count
    # 顺带封一个上界，挡住"条数是常数但常数很大"的实现
    assert many.count <= 3, many.statements


def test_run_list_pages_stably(client: TestClient, session: Session) -> None:
    """翻页不重复不漏行 —— 排序键是唯一的 id（AC-4）。"""
    seeded = seed_minimal(session, tasks=1)
    for _ in range(5):
        create_runs(
            session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
        )
    session.commit()

    first = client.get("/api/runs", params={"limit": 2, "offset": 0}).json()
    second = client.get("/api/runs", params={"limit": 2, "offset": 2}).json()
    third = client.get("/api/runs", params={"limit": 2, "offset": 4}).json()

    ids = [r["id"] for page in (first, second, third) for r in page["items"]]
    assert len(ids) == 5
    assert len(set(ids)) == 5
    assert ids == sorted(ids, reverse=True)


def test_run_detail_carries_the_manifest(client: TestClient, session: Session) -> None:
    seeded = seed_minimal(session, tasks=1)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    session.commit()

    body = client.get(f"/api/runs/{run.id}").json()

    assert body["id"] == run.id
    assert body["manifest"]["protocol_version"]
    assert body["dirty"] is False
    assert body["leaderboard_excluded_reason"] is None


def test_unknown_run_is_404_with_the_standard_error_shape(client: TestClient) -> None:
    response = client.get("/api/runs/987654")

    assert response.status_code == 404
    assert response.json() == {"code": "RUN_NOT_FOUND", "message": "找不到实验 #987654"}


def test_task_run_grid_query_count_does_not_grow_with_rows(
    client: TestClient, session: Session, engine: Engine
) -> None:
    """任务网格的 SQL 条数不随格子数增长（AC-7 点名的就是这个）。

    22 道题的网格，每格要显示题号。逐格再查一次题目表就是 23 条查询 ——
    功能上完全正常，只是慢，而且慢得没有症状。
    """
    seeded = seed_minimal(session, tasks=20)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]

    def add_task_runs(task_ids: tuple[int, ...]) -> None:
        for task_id in task_ids:
            session.add(
                EvaluationTaskRun(
                    evaluation_run_id=run.id,
                    benchmark_task_id=task_id,
                    lifecycle_status=LifecycleStatus.QUEUED,
                )
            )
        session.commit()

    add_task_runs(seeded.task_ids[:2])
    with count_queries(engine) as few:
        small = client.get(f"/api/runs/{run.id}/task-runs").json()

    add_task_runs(seeded.task_ids[2:])
    with count_queries(engine) as many:
        big = client.get(f"/api/runs/{run.id}/task-runs").json()

    assert small["total"] == 2
    assert big["total"] == 20
    assert all(item["task_id"] for item in big["items"])
    assert few.count == many.count
    assert many.count <= 4, many.statements


def test_task_runs_of_an_unknown_run_is_404_not_an_empty_list(client: TestClient) -> None:
    """实验不存在时要 404。返回空列表的话，前端分不清"没有子任务"和"实验不存在"。"""
    response = client.get("/api/runs/987654/task-runs")

    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_enum_values_pass_through_unmapped(client: TestClient, session: Session) -> None:
    """三个状态字段原样透出，不合并也不做中文映射（协议 C-04/C-05/C-06，AC-10）。"""
    seeded = seed_minimal(session, tasks=1)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    session.add(
        EvaluationTaskRun(
            evaluation_run_id=run.id,
            benchmark_task_id=seeded.task_ids[0],
            lifecycle_status=LifecycleStatus.AGENT_RUNNING,
        )
    )
    session.commit()

    item = client.get(f"/api/runs/{run.id}/task-runs").json()["items"][0]

    assert item["lifecycle_status"] == "AGENT_RUNNING"
    assert item["infra_outcome"] is None
    assert item["agent_outcome"] is None
    assert client.get(f"/api/runs/{run.id}").json()["status"] == "QUEUED"


# ── 取消与补跑 ──────────────────────────────────────────────


def test_cancel_drops_pending_jobs(client: TestClient, session: Session, admin_token: str) -> None:
    """取消转给 `cancel_run()`：实验标 CANCELLED，没被领走的作业变 DEAD。"""
    seeded = seed_minimal(session, tasks=3)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    session.commit()

    response = client.post(f"/api/runs/{run.id}/cancel", headers={"X-Bench-Token": admin_token})

    assert response.status_code == 200
    assert response.json()["dropped_jobs"] == 3
    session.expire_all()
    assert session.get(EvaluationRun, run.id).status is EvaluationRunStatus.CANCELLED
    dead = session.execute(
        sa.select(sa.func.count()).where(
            JobQueue.payload["evaluation_run_id"].astext == str(run.id),
            JobQueue.state == JobState.DEAD,
        )
    ).scalar_one()
    assert dead == 3


def test_cancel_a_completed_run_is_409(
    client: TestClient, session: Session, admin_token: str
) -> None:
    """跑完的实验没什么可取消的 —— 409，不是 400 也不是 500。"""
    seeded = seed_minimal(session, tasks=1)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    run.status = EvaluationRunStatus.COMPLETED
    session.commit()

    response = client.post(f"/api/runs/{run.id}/cancel", headers={"X-Bench-Token": admin_token})

    assert response.status_code == 409
    assert response.json()["code"] == "RUN_ALREADY_FINISHED"


def test_retry_failed_on_a_completed_run_is_409(
    client: TestClient, session: Session, admin_token: str
) -> None:
    """COMPLETED 的实验每道题都有结论，没有洞可补；要再跑得新建（协议 C-55）。"""
    seeded = seed_minimal(session, tasks=1)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    run.status = EvaluationRunStatus.COMPLETED
    session.commit()

    response = client.post(
        f"/api/runs/{run.id}/retry-failed", headers={"X-Bench-Token": admin_token}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "RUN_ALREADY_COMPLETED"
    assert "C-55" in response.json()["message"]


def test_retry_failed_requeues_the_holes(
    client: TestClient, session: Session, admin_token: str
) -> None:
    """补跑转给 `retry_failed()`：作业被判 DEAD 的那道题重新投一条。"""
    seeded = seed_minimal(session, tasks=2)
    run = create_runs(
        session, name="n", task_ids=list(seeded.task_ids), provenance=provenance_for(seeded)
    )[0]
    session.execute(
        sa.update(JobQueue)
        .where(JobQueue.payload["evaluation_run_id"].astext == str(run.id))
        .values(state=JobState.DEAD)
    )
    session.commit()

    response = client.post(
        f"/api/runs/{run.id}/retry-failed", headers={"X-Bench-Token": admin_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["requeued"]) == sorted(seeded.task_ids)
    assert body["already_decided"] == 0


def test_invalid_parameter_is_422_with_the_standard_shape(client: TestClient) -> None:
    """参数不合法也是 {code, message}，不是 FastAPI 默认的 detail 数组。"""
    response = client.get("/api/runs", params={"limit": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_PARAMETER"
    assert "limit" in body["message"]
