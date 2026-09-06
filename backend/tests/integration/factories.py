"""集成测试用的最小种子数据（E5-T2）。

编排层的测试要的东西是一样的：一个数据集、一个 Agent 配置、若干道题。
每个测试文件各抄一份的话，加一个非空列就要改好几处，而且总会漏掉一处。

**只建外键链路需要的最少字段。** 这里不是在演示真实数据长什么样，
真实数据长什么样由 `datasets/golden/` 和 `cli.queue seed-golden` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind, IssueLanguage, TaskDifficulty
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
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


def wipe(session: Session) -> None:
    """按外键顺序清干净。测试之间互不干扰靠它。"""
    for table in (
        JobQueue,
        TestResult,
        PatchArtifact,
        EvaluationTaskRun,
        EvaluationRun,
        BenchmarkTask,
        BenchmarkSet,
        EnvironmentSpec,
        AgentConfig,
        Agent,
        Repository,
    ):
        session.execute(sa.delete(table))
    session.commit()


__all__ = ["Seeded", "seed_minimal", "wipe"]
