"""Worker 真跑一道题、真起容器、真收残留（E5-T1 的验收标准）。

**要 Docker，也要数据库。** 这里是 E5-T1 唯一一处两样都要的测试，因为它验的正是
那三条断言里没法用假对象代替的部分：

| 验收标准 | 在这里的哪一条 |
|:---|:---|
| 队列跑得通一道真题 | `test_a_real_task_runs_end_to_end_through_the_queue` |
| 启动时清掉上一条命的残留 | `test_startup_reaps_a_leftover_container` |
| 放弃等待之后也要清干净 | `test_shutdown_reaps_containers_left_by_an_abandoned_handler` |
| SIGTERM 后无残留容器 | `test_sigterm_leaves_no_containers` |

前面那些不带 `docker` 标记的测试用假处理函数验调度、用手工结果验落库。
这一条把它们串起来跑一遍真的 —— 中间任何一处接错了，只有这里会红。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import AgentOutcome, InfraOutcome, JobState, JobType, LifecycleStatus
from app.infrastructure.config import REPO_ROOT, Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.infrastructure.models.job import JobQueue
from app.sandbox.container import BENCH_LABEL, BENCH_LABEL_VALUE, get_docker_client
from app.worker.handlers import default_registry, enqueue_eval_task
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry, JobContext

pytestmark = [pytest.mark.docker, pytest.mark.db]

#: 拿哪道 Golden 题来跑。随便挑一道，四道都一样。
TASK_ID = "bench-golden__textkit-1"

#: 起残留容器用的镜像。`make images` 建好的那个。
GOLDEN_IMAGE = "bench-golden:py311"


def bench_containers(client: object) -> list[object]:
    """当前所有带 bench 标签的容器（含已停止的）。"""
    return list(
        client.containers.list(  # type: ignore[attr-defined]
            all=True, filters={"label": f"{BENCH_LABEL}={BENCH_LABEL_VALUE}"}
        )
    )


@pytest.fixture
def docker_client() -> object:
    try:
        client = get_docker_client()
        client.ping()
    except Exception as exc:
        pytest.skip(f"连不上 Docker：{exc}")
    return client


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """连本机数据库。

    `tests/integration/conftest.py` 里那个同名夹具管不到这里（conftest 按目录生效），
    所以本地再来一个。**不跑迁移** —— 那个夹具会先 `downgrade base` 再 `upgrade head`，
    在这里做等于把库清空，而这一组要用 `make seed` 写进去的哨兵 Agent。
    """
    eng = create_db_engine()
    try:
        with eng.connect() as conn:
            conn.execute(sa.text("SELECT 1 FROM job_queue LIMIT 1"))
    except OperationalError as exc:
        pytest.skip(f"连不上数据库，先跑 `make db-up`（{exc.__class__.__name__}）")
    except ProgrammingError:
        pytest.skip("库里没有 job_queue 表，先跑 `make migrate`")
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "worker_id": "e5t1-docker-test",
            "job_poll_interval_s": 0.1,
            "job_retry_backoff_base_s": 0.01,
            "worker_shutdown_grace_s": 3.0,
        }
    )


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> Iterator[tuple[int, int]]:
    """确保库里有 Golden 题和 oracle 配置，返回 `(evaluation_run_id, benchmark_task_id)`。

    直接调 CLI 里那两个函数，不另写一套建行的代码 —— 演示时用的就是它们，
    测试里换一套的话，测过的和演示的就不是同一条路径了。
    """
    from app.benchmark.schema import TaskDefinition
    from app.infrastructure.models.agent import Agent, AgentConfig
    from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask
    from cli.queue import GOLDEN_SET_SLUG, load_environments, upsert_task
    from cli.seed import seed_agents

    golden_file = REPO_ROOT / "datasets" / "golden" / f"{TASK_ID}.json"
    if not golden_file.is_file():
        pytest.skip(f"{golden_file} 不在，先跑 `make golden`")

    with factory() as session:
        seed_agents(session)
        dataset = session.execute(
            sa.select(BenchmarkSet).where(BenchmarkSet.slug == GOLDEN_SET_SLUG)
        ).scalar_one_or_none()
        if dataset is None:
            dataset = BenchmarkSet(slug=GOLDEN_SET_SLUG, version="v1", title="Golden Tasks")
            session.add(dataset)
            session.flush()
        task = TaskDefinition.model_validate_json(golden_file.read_text(encoding="utf-8"))
        upsert_task(session, task, load_environments())

        config = session.execute(
            sa.select(AgentConfig).join(Agent).where(Agent.name == "oracle")
        ).scalar_one()
        task_id = session.execute(
            sa.select(BenchmarkTask.id).where(BenchmarkTask.task_id == TASK_ID)
        ).scalar_one()
        run = EvaluationRun(
            name="e5t1-docker", benchmark_set_id=dataset.id, agent_config_id=config.id
        )
        session.add(run)
        session.flush()
        run_id = run.id
        session.commit()

    yield run_id, task_id

    with factory() as session:
        session.execute(sa.delete(JobQueue))
        session.execute(
            sa.delete(EvaluationTaskRun).where(EvaluationTaskRun.evaluation_run_id == run_id)
        )
        session.execute(sa.delete(EvaluationRun).where(EvaluationRun.id == run_id))
        session.commit()


# ── 真跑一道题 ──────────────────────────────────────────────


def test_a_real_task_runs_end_to_end_through_the_queue(
    factory: sessionmaker[Session],
    settings: Settings,
    seeded: tuple[int, int],
    docker_client: object,
) -> None:
    """投一条作业 → Worker 领走 → 真起容器跑测试 → 判定 → 落库 → 作业 DONE。

    用 Oracle 哨兵跑：它交的是官方补丁，所以在一道健康的题上**必须** RESOLVED
    （协议 C-50）。不是 RESOLVED 就说明这条链上有一环接错了，而这正是
    E4-T4 到 E5-T1 之间最容易出错的地方。
    """
    run_id, task_id = seeded
    with factory() as session:
        job_id = enqueue_eval_task(session, evaluation_run_id=run_id, benchmark_task_id=task_id).id
        session.commit()

    worker = Worker(default_registry(), settings=settings, session_factory=factory)
    assert worker.run_once() is True

    with factory() as session:
        row = session.execute(
            sa.select(EvaluationTaskRun).where(EvaluationTaskRun.evaluation_run_id == run_id)
        ).scalar_one()
        # 按 id 取这一条，不是 `select(JobQueue).scalar_one()` —— 这是台开发机，
        # 表里可能还留着别的实验投的作业
        job = session.get(JobQueue, job_id)
    assert job is not None

    assert row.attempt_no == 1
    assert row.lifecycle_status is LifecycleStatus.COMPLETED
    assert row.infra_outcome is InfraOutcome.SUCCESS
    assert row.agent_outcome is AgentOutcome.RESOLVED, "Oracle 交官方补丁，必须解出来"
    assert row.is_canonical is True
    assert row.worker_id == "e5t1-docker-test"
    assert row.f2p_total and row.f2p_passed == row.f2p_total, "F2P 必须全过"
    assert row.p2p_total and row.p2p_passed == row.p2p_total, "P2P 不能被改坏"
    assert job.state is JobState.DONE

    assert bench_containers(docker_client) == [], "跑完不该留下容器"


# ── 残留容器回收 ────────────────────────────────────────────


def test_startup_reaps_a_leftover_container(
    factory: sessionmaker[Session], settings: Settings, docker_client: object
) -> None:
    """启动时把上一条命留下的容器清掉。

    `kill -9` 的时候 `run_in_container` 的 `finally` 根本跑不到，容器会一直
    占着内存和 pid。不清的话，下一批评测会因为资源不够而莫名其妙地失败 ——
    而失败原因指向的是新任务，不是那个已经死掉的 Worker。
    """
    leftover = docker_client.containers.run(  # type: ignore[attr-defined]
        GOLDEN_IMAGE,
        ["sleep", "300"],
        detach=True,
        labels={BENCH_LABEL: BENCH_LABEL_VALUE},
        network_mode="none",
    )
    try:
        assert any(c.id == leftover.id for c in bench_containers(docker_client))

        worker = Worker(default_registry(), settings=settings, session_factory=factory)
        assert worker.reap_orphan_containers() >= 1

        assert not any(c.id == leftover.id for c in bench_containers(docker_client))
    finally:
        # 上面删成功了就没得删了，这是正常路径，不是异常
        with contextlib.suppress(Exception):
            leftover.remove(force=True)


def test_shutdown_reaps_containers_left_by_an_abandoned_handler(
    factory: sessionmaker[Session], settings: Settings, docker_client: object
) -> None:
    """宽限期用完、放弃等待处理函数之后，退出前还是要把容器收掉。

    这是"停机后无残留容器"里最难的那种情况：处理函数卡在一个起了容器的调用上
    （比如 docker daemon 假死），我们等不下去了。删掉它正在用的容器是**故意的** ——
    进程马上就要退出，那个线程也活不过进程，容器留下来只会一直占资源。
    """
    started = threading.Event()

    def wedged(_ctx: JobContext) -> None:
        docker_client.containers.run(  # type: ignore[attr-defined]
            GOLDEN_IMAGE,
            ["sleep", "300"],
            detach=True,
            labels={BENCH_LABEL: BENCH_LABEL_VALUE},
            network_mode="none",
        )
        started.set()
        time.sleep(120)  # 卡住不返回

    from app.infrastructure import queue

    with factory() as session:
        queue.enqueue(session, job_type=JobType.EVAL_TASK, payload={})
        session.commit()

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: wedged}),
        settings=settings.model_copy(update={"worker_shutdown_grace_s": 1.0}),
        session_factory=factory,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=60), "处理函数没起来"
        worker.request_stop()
        thread.join(timeout=60)
        assert not thread.is_alive(), "宽限期到了还没退出"
        assert bench_containers(docker_client) == [], "退出前必须把残留容器收掉"
    finally:
        worker.request_stop(force=True)
        with factory() as session:
            session.execute(sa.delete(JobQueue))
            session.commit()
        for container in bench_containers(docker_client):
            container.remove(force=True)  # type: ignore[attr-defined]


# ── 真发一次 SIGTERM ────────────────────────────────────────


def test_sigterm_leaves_no_containers(
    factory: sessionmaker[Session], seeded: tuple[int, int], docker_client: object
) -> None:
    """**验收标准**：起一个真的 Worker 进程，跑完一道题，`kill -TERM`，不留残留容器。

    前面几条都是在同一个进程里模拟停机。这一条走的是真路径：
    `python -m app.worker` 起进程、`os.kill(SIGTERM)` 发信号、等它自己退出。
    信号处理器要是没装上，或者装了但主循环收不到，只有这条会红。
    """
    run_id, task_id = seeded
    with factory() as session:
        enqueue_eval_task(session, evaluation_run_id=run_id, benchmark_task_id=task_id)
        session.commit()

    env = {**os.environ, "WORKER_ID": "e5t1-sigterm", "JOB_POLL_INTERVAL_S": "0.1"}
    process = subprocess.Popen(
        [str(Path(REPO_ROOT) / "backend" / ".venv" / "bin" / "python"), "-m", "app.worker"],
        cwd=str(REPO_ROOT / "backend"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            with factory() as session:
                done = session.execute(
                    sa.select(sa.func.count())
                    .select_from(JobQueue)
                    .where(JobQueue.state == JobState.DONE)
                ).scalar_one()
            if done >= 1:
                break
            time.sleep(0.5)
        else:
            pytest.fail("Worker 进程 180 秒内没把作业跑完")

        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode == 0, f"Worker 没有干净退出：\n{stdout}"
    assert "worker_stopped" in stdout, f"没走到收尾那一步：\n{stdout}"
    assert bench_containers(docker_client) == [], "SIGTERM 之后不该有残留容器"

    with factory() as session:
        stuck = session.execute(
            sa.select(sa.func.count())
            .select_from(JobQueue)
            .where(JobQueue.state == JobState.LEASED)
        ).scalar_one()
    assert stuck == 0, "退出时不该有作业还挂在 LEASED 上"
