"""全部 ORM 模型。

导入本模块就等于把所有表注册进 `Base.metadata` —— Alembic 的 env.py
和测试建表都依赖这一点，所以这里必须把每张表都显式导出，
少一张就会在迁移里悄悄消失。
"""

from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import (
    FailureAttribution,
    HumanReview,
    ReportRecord,
)
from app.infrastructure.models.base import Base
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

__all__ = [
    "Agent",
    "AgentConfig",
    "Artifact",
    "Base",
    "BenchmarkSet",
    "BenchmarkSetItem",
    "BenchmarkTask",
    "EnvironmentSpec",
    "EvaluationRun",
    "EvaluationTaskRun",
    "FailureAttribution",
    "HumanReview",
    "JobQueue",
    "PatchArtifact",
    "ReportRecord",
    "Repository",
    "TaskCandidate",
    "TestResult",
]
