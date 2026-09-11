"""集成测试用的最小种子数据（E5-T2）。

编排层的测试要的东西是一样的：一个数据集、一个 Agent 配置、若干道题。
每个测试文件各抄一份的话，加一个非空列就要改好几处，而且总会漏掉一处。

**只建外键链路需要的最少字段。** 这里不是在演示真实数据长什么样，
真实数据长什么样由 `datasets/golden/` 和 `cli.queue seed-golden` 负责。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind, IssueLanguage, TaskDifficulty
from app.evaluation.manifest import AgentRef, DatasetRef, RunProvenance
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkSetItem,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
    TaskCandidate,
)
from app.infrastructure.models.evaluation import (
    EvaluationRun,
    EvaluationTaskRun,
    PatchArtifact,
    TestResult,
)
from app.infrastructure.models.job import JobQueue


@dataclass(frozen=True, slots=True)
class Seeded:
    benchmark_set_id: int
    agent_config_id: int
    task_ids: tuple[int, ...]


def seed_minimal(session: Session, *, tasks: int = 1, slug: str = "golden") -> Seeded:
    """建好一条完整的外键链路，返回下游要用的三个 id。调用方负责 commit。"""
    repo = Repository(full_name="bench-golden/textkit", url="golden://x", language="python")
    session.add(repo)
    session.flush()

    env = EnvironmentSpec(
        environment_id=f"{slug}__textkit__py311",
        repository_id=repo.id,
        python_version="3.11",
        install_command="python -m pip install pytest",
        test_command="python -m pytest",
        test_report_path="report/junit.xml",
    )
    dataset = BenchmarkSet(slug=slug, version="v1", title="Golden Tasks")
    agent = Agent(name="mock", display_name="Mock", kind=AgentKind.MOCK, adapter_class="MockRunner")
    session.add_all([env, dataset, agent])
    session.flush()

    config = AgentConfig(
        agent_id=agent.id,
        label="mock-default",
        agent_version="1.0",
        model_name="none",
        config_hash="0" * 64,
    )
    session.add(config)
    session.flush()

    task_ids: list[int] = []
    for index in range(1, tasks + 1):
        task = BenchmarkTask(
            task_id=f"bench-golden__textkit-{index}",
            repository_id=repo.id,
            environment_spec_id=env.id,
            base_commit="a" * 40,
            issue_title=f"标题 {index}",
            issue_body="正文",
            issue_language=IssueLanguage.ZH,
            fail_to_pass=["tests/test_a.py::test_new"],
            pass_to_pass=[],
            test_patch_uri="local://test.patch",
            test_patch_paths=["tests/test_a.py"],
            gold_patch_uri="local://gold.patch",
            difficulty=TaskDifficulty.EASY,
            content_hash=f"{index}" * 64,
            raw_definition={},
        )
        session.add(task)
        session.flush()
        task_ids.append(task.id)

    return Seeded(
        benchmark_set_id=dataset.id,
        agent_config_id=config.id,
        task_ids=tuple(task_ids),
    )


def provenance_for(
    seeded: Seeded,
    *,
    task_ids: Sequence[int] | None = None,
    agent_concurrency: int = 4,
    sandbox_concurrency: int = 2,
    job_max_attempts: int = 3,
    harness_git_sha: str = "0" * 40,
    dirty: bool = False,
) -> RunProvenance:
    """测试用的可复现性凭证：**不调 git、不连 docker**。

    生产路径必须走 `app.evaluation.manifest.collect_provenance()` —— 那里才是
    协议 C-27（脏工作区拒绝启动）的强制点。测试直接构造，理由很实在：
    开发时工作区**永远是脏的**，让每个集成测试都去查一次 `git status`，
    它们会集体变红，而这和被测的东西一点关系都没有。
    """
    ids = tuple(task_ids if task_ids is not None else seeded.task_ids)
    return RunProvenance(
        harness_git_sha=harness_git_sha,
        dirty=dirty,
        dataset=DatasetRef(
            slug="golden",
            version="v1",
            benchmark_set_id=seeded.benchmark_set_id,
            snapshot_task_count=len(seeded.task_ids),
            selected_task_count=len(ids),
            snapshot_digest="sha256:" + "1" * 64,
        ),
        agent=AgentRef(
            agent_config_id=seeded.agent_config_id,
            name="mock",
            label="mock-default",
            agent_version="1.0",
            model_name="none",
            config_hash="0" * 64,
            adapter_class="MockRunner",
            params={},
        ),
        images=(),
        limits={
            "agent_concurrency": agent_concurrency,
            "sandbox_concurrency": sandbox_concurrency,
            "job_max_attempts": job_max_attempts,
        },
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        host=None,
    )


def wipe(session: Session) -> None:
    """按外键顺序清干净。测试之间互不干扰靠它。"""
    for table in (
        JobQueue,
        TestResult,
        PatchArtifact,
        EvaluationTaskRun,
        EvaluationRun,
        # 必须排在 BenchmarkTask 前面：这张表指向题目的外键是 RESTRICT，
        # 反过来删会直接报违反外键（E1-T6 加）
        BenchmarkSetItem,
        BenchmarkTask,
        BenchmarkSet,
        EnvironmentSpec,
        AgentConfig,
        Agent,
        # 外键上有 ON DELETE CASCADE，删 Repository 时它会跟着走；
        # 仍然显式列出来，是为了让"这张表也归 wipe 管"在代码里看得见（E1-T4）
        TaskCandidate,
        Repository,
    ):
        session.execute(sa.delete(table))
    session.commit()


__all__ = ["Seeded", "provenance_for", "seed_minimal", "wipe"]
