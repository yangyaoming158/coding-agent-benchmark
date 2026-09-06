"""评测域的表：实验、单题单次执行、补丁、逐条用例结果。

`evaluation_task_runs` 是全库最关键的一张表，协议里关于状态字段的规定
基本都落在它身上。它上面挂了三条 CHECK 约束，作用是让"违反协议的记录"
在写库这一层就写不进去，而不是等报表算出来才发现不对。
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AgentOutcome,
    CostSource,
    EvaluationRunStatus,
    InfraOutcome,
    LifecycleStatus,
    PatchKind,
    TestRole,
    TestStatus,
)
from app.domain.protocol import LEGAL_COMBINATIONS, PROTOCOL_VERSION
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


def _legal_combination_sql() -> str:
    """把协议 §4.3 的合法组合表编译成一条 SQL 布尔表达式。

    **这段 SQL 是生成的，不是手写的。** 协议改了合法组合表，
    `app.domain.protocol.LEGAL_COMBINATIONS` 跟着改，这里自动跟着变，
    然后 Alembic 会检测到约束变化。中间没有需要人记得同步的环节。

    生成出来的形状：

        CASE WHEN lifecycle_status IN (非终态...) THEN agent_outcome IS NULL
             ELSE (19 个合法终态组合 OR 起来)
        END

    为什么不用 `(a, b, c) IN ((...), ...)` 这种紧凑写法：agent_outcome 有
    NULL 取值，而 SQL 里 `(x, y, NULL) IN ((x, y, NULL))` 的结果是 NULL 不是 TRUE，
    这些行会被静默漏掉。只能老老实实展开成 `IS NULL` 与 `= '值'` 的组合。
    """
    # 按枚举声明顺序取非终态，不用 frozenset —— 集合的迭代顺序每次进程启动都可能变，
    # 会让生成的 SQL 不稳定，Alembic 每次都以为约束改了。
    terminal = ("COMPLETED", "FAILED", "CANCELLED")
    non_terminal = [s.value for s in LifecycleStatus if s.value not in terminal]
    non_terminal_list = ", ".join(f"'{v}'" for v in non_terminal)

    clauses: list[str] = []
    for combo in LEGAL_COMBINATIONS:
        # 用 IS NOT DISTINCT FROM 而不是 = ：infra_outcome 和 agent_outcome 都可为空，
        # 而 SQL 里 `NULL = '值'` 的结果是 NULL 不是 FALSE。整串 OR 里只要有一项是
        # NULL、其余都是 FALSE，结果就是 NULL —— 而 CHECK 约束遇到 NULL 是**放行**的。
        # 这个坑已经在本机复现过：agent_outcome 为空的非法组合被静默放了进去。
        parts = [
            f"lifecycle_status = '{combo.lifecycle_status.value}'",
            f"infra_outcome IS NOT DISTINCT FROM '{combo.infra_outcome.value}'",
        ]
        if combo.agent_outcome is None:
            parts.append("agent_outcome IS NULL")
        else:
            parts.append(f"agent_outcome IS NOT DISTINCT FROM '{combo.agent_outcome.value}'")
        # 协议 §4.3"区分条件"那一列里关于 agent_started_at 的要求（C-69、C-77）。
        if combo.agent_started is True:
            parts.append("agent_started_at IS NOT NULL")
        elif combo.agent_started is False:
            parts.append("agent_started_at IS NULL")
        clauses.append("(" + " AND ".join(parts) + ")")

    return (
        f"CASE WHEN lifecycle_status IN ({non_terminal_list}) "
        f"THEN agent_outcome IS NULL "
        f"ELSE ({' OR '.join(clauses)}) END"
    )


class EvaluationRun(Base):
    """一次实验 = 一个 Agent 配置 × 一个数据集版本。"""

    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    benchmark_set_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("benchmark_sets.id", ondelete="RESTRICT"), nullable=False
    )
    agent_config_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("agent_configs.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[EvaluationRunStatus] = mapped_column(
        pg_enum(EvaluationRunStatus), nullable=False, default=EvaluationRunStatus.DRAFT
    )

    #: 两层并发（ADR-012）。AGENT 侧是等大模型响应，属于 IO 密集；
    #: SANDBOX 侧是跑测试，属于 CPU 和内存密集。合成一个数字调不出好配置。
    agent_concurrency: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=8)
    sandbox_concurrency: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=4)

    total_tasks: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    completed_tasks: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    resolved_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    infra_failure_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    #: 严格解决率 = RESOLVED 题数 / 题库总题数。排行榜展示这个（协议 C-22）。
    strict_resolve_rate: Mapped[Decimal | None] = mapped_column(sa.Numeric(6, 4))
    #: 有效解决率，分母是"确实拿到了可归因于 AI 的结果"的题数（协议 C-21）。只用于自己诊断。
    effective_resolve_rate: Mapped[Decimal | None] = mapped_column(sa.Numeric(6, 4))

    total_cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
    )
    total_tokens: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    #: 从第一道题开始跑到最后一道题结束的总墙钟时间，不是所有题耗时之和。
    makespan_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: 其中花在等外部服务（大模型 API、限流退避）上的时间。
    #: 单列出来才能把"平台吞吐不行"和"外部限流"分开，否则性能报告没法解释。
    external_wait_ms: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )

    #: 创建实验时写入的协议版本号，**禁止**事后修改（协议 C-67）。
    #: 协议改版后旧结果不重算，但报告里要能说清它当时依据的是哪一版。
    protocol_version: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text(f"'{PROTOCOL_VERSION}'")
    )
    #: 该实验所有题目的 attempt 总数减去题数。
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: 重试之后恢复正常的平台故障次数。
    #:
    #: 这两个数要在报告里单独展示，它们回答一个重要问题：这次实验到底顺不顺利。
    #: 解决率同样是 40%，零重试的和重试 30 次才凑齐的，可信度完全不同（协议 C-56）。
    recovered_infra_failure_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )

    #: 工作区有未提交改动时启动的实验（协议 C-28 的 --allow-dirty）。
    #: 标了 dirty 的结果不得进入排行榜 —— 因为记录的代码版本号已经对不上实际代码了。
    dirty: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())

    #: 可复现性清单：镜像 digest 表、harness 的 git sha、数据集哈希、
    #: 环境变量白名单、随机种子。结构会演化且不需要 join 查询，所以放 JSONB。
    manifest: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(sa.String(100))
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.Index("ix_evaluation_runs_status", "status"),
        sa.Index("ix_evaluation_runs_set_config", "benchmark_set_id", "agent_config_id"),
    )


class EvaluationTaskRun(Base):
    """单题单次执行 —— 核心宽表。

    故意做成一张宽表而不是拆成 5 张阶段表：查一次评测的完整经过是最高频的操作
    （前端的"3 次点击看到失败证据"这条主线全靠它），拆表会让每次都要 join 五次。
    量级也撑得住 —— 300 次实验 × 300 题 ≈ 9 万行。
    """

    __tablename__ = "evaluation_task_runs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_run_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
    )
    benchmark_task_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("benchmark_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    #: 第几次尝试，从 1 开始。重试是新建一条记录，**禁止**把已有记录的状态改回去（协议 C-32）。
    attempt_no: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)

    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus), nullable=False, default=LifecycleStatus.QUEUED
    )
    #: 非终态时还不知道跑得对不对，所以可空。
    infra_outcome: Mapped[InfraOutcome | None] = mapped_column(pg_enum(InfraOutcome))
    #: 非终态时一律为 NULL（协议 C-09）。终态时按 C-68 的合法组合表取值。
    agent_outcome: Mapped[AgentOutcome | None] = mapped_column(pg_enum(AgentOutcome))

    queued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    prepare_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: 置位时刻的定义（协议 C-77）：**Agent 容器成功启动、且任务输入已写入其标准输入**的那一刻。
    #:
    #: 定这么死是因为整张合法组合表都靠这个字段区分"没给 AI 机会"和
    #: "给了机会但我们没拿到结论"。举个会分歧的例子：鉴权失败时，
    #: probe 阶段就发现密钥无效算未启动，容器跑起来后调 API 才拿到 401 算已启动。
    agent_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    agent_finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    test_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    test_finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    judged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    agent_duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    test_duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    total_duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    exit_code: Mapped[int | None] = mapped_column(sa.Integer)

    tokens_input: Mapped[int | None] = mapped_column(sa.BigInteger)
    tokens_output: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: 提示缓存命中的 token 数。**是 `tokens_input` 的一部分，不是另加的**，
    #: 所以不进 `tokens_total` —— 加进去 token 统计会凭空多出一截。
    #:
    #: 单独记一列是因为它是成本分析绕不开的一环：DeepSeek 的缓存命中便宜一个数量级，
    #: 不记的话，"两次运行 token 差不多、钱差好几倍"这种事解释不了，
    #: 而按 token 估算成本（协议纪律 3 的 `estimated` 那条路）会系统性偏高。
    tokens_cache_read: Mapped[int | None] = mapped_column(sa.BigInteger)
    tokens_total: Mapped[int | None] = mapped_column(sa.BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    cost_source: Mapped[CostSource | None] = mapped_column(pg_enum(CostSource))
    #: Agent 与模型交互的轮数，超时归因（F8）会用到。
    turns: Mapped[int | None] = mapped_column(sa.Integer)

    #: 指向标准化后的补丁。用 use_alter 建外键：本表和 patch_artifacts 互相引用，
    #: 不加的话建表顺序会死锁。
    patch_artifact_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("patch_artifacts.id", ondelete="SET NULL", use_alter=True),
    )
    files_changed: Mapped[int | None] = mapped_column(sa.Integer)
    lines_added: Mapped[int | None] = mapped_column(sa.Integer)
    lines_deleted: Mapped[int | None] = mapped_column(sa.Integer)

    f2p_passed: Mapped[int | None] = mapped_column(sa.Integer)
    f2p_total: Mapped[int | None] = mapped_column(sa.Integer)
    p2p_passed: Mapped[int | None] = mapped_column(sa.Integer)
    p2p_total: Mapped[int | None] = mapped_column(sa.Integer)

    error_code: Mapped[str | None] = mapped_column(sa.String(100))
    #: 只存摘要。完整日志走制品存储，可达数 MB，不入库。
    error_message_excerpt: Mapped[str | None] = mapped_column(sa.String(2000))
    worker_id: Mapped[str | None] = mapped_column(sa.String(100))
    retry_of_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_task_runs.id", ondelete="SET NULL")
    )

    #: 这次 attempt 是不是被选为统计依据的那一次（协议 C-24、C-57）。
    #:
    #: 必须是显式字段，**禁止**靠"取最大的 attempt_no"临时推断（协议 C-58）。
    #: 举例：第 1 次就遇到 AGENT_TIMEOUT，按 C-18 它不可重试，那它就是认定结果，
    #: 哪怕后面因为别的原因又产生了记录。取最大编号会算错。
    is_canonical: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    #: 下面三个是诊断字段（协议 C-08b）。它们存在的理由是 EMPTY_PATCH 有歧义：
    #: 它的含义是"标准化之后补丁为空"，不等于"AI 什么都没做"。
    #: AI 改了一堆受保护文件想蒙混过关、被平台全部丢弃，结果也是空补丁。
    #: 不记这三个字段，失败分析会把这两种截然不同的行为混为一谈。
    raw_patch_empty: Mapped[bool | None] = mapped_column(sa.Boolean)
    #: 为 true 时**本身就要触发人工复核**，即使最终没有出现 MISSING（协议 C-13d）。
    protected_path_edit_attempted: Mapped[bool | None] = mapped_column(sa.Boolean)
    #: 哪些改动被丢弃了、分别因为什么（受保护路径 / 二进制 / 超大文件 / 空 mode 变更）。
    filtered_change_reasons: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        # 协议 C-48。
        sa.UniqueConstraint(
            "evaluation_run_id", "benchmark_task_id", "attempt_no", name="uq_task_run_attempt"
        ),
        # 协议 C-57：保证每题至多一个认定结果。用部分唯一索引而不是普通唯一约束 ——
        # 同一道题会有多条 attempt 记录，只有 is_canonical 为真的那条要唯一。
        sa.Index(
            "uq_task_run_canonical",
            "evaluation_run_id",
            "benchmark_task_id",
            unique=True,
            postgresql_where=sa.text("is_canonical"),
        ),
        # 协议 C-09、C-29、C-30、C-68、C-69、C-77、C-78 的数据库版本。
        # 表达式由 LEGAL_COMBINATIONS 生成，见本文件顶部的 _legal_combination_sql。
        sa.CheckConstraint(_legal_combination_sql(), name="legal_combination"),
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        sa.Index("ix_evaluation_task_runs_run_lifecycle", "evaluation_run_id", "lifecycle_status"),
        sa.Index("ix_evaluation_task_runs_benchmark_task_id", "benchmark_task_id"),
        sa.Index("ix_evaluation_task_runs_agent_outcome", "agent_outcome"),
    )


class PatchArtifact(Base):
    """一次执行产生的补丁及其统计。

    原始补丁和标准化补丁两份都存：只存后者的话，"AI 试图改测试文件"
    这个行为就再也查不到了，而它是防作弊分析的主要证据。
    """

    __tablename__ = "patch_artifacts"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_task_run_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_task_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[PatchKind] = mapped_column(pg_enum(PatchKind), nullable=False)
    uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    files_changed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    lines_added: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    lines_deleted: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    is_empty: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    applies_cleanly: Mapped[bool | None] = mapped_column(sa.Boolean)
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.UniqueConstraint("evaluation_task_run_id", "kind", name="uq_patch_artifacts_run_kind"),
    )


class TestResult(Base):
    """逐条用例的结果 —— 判定的证据。

    量级：300 次实验 × 约 50 条用例 ≈ 1.5 万行每次实验，数据库完全无压力。
    逐条入库是"结论可查"的基础，也是失败归因第二步的数据来源。
    """

    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_task_run_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_task_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: 归一化之后的用例 ID。归一化写错是本项目公认最容易出的静默 bug ——
    #: 它会制造大量假 MISSING，看起来像作弊，其实是我们自己的解析器错了（协议 C-13a）。
    test_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    role: Mapped[TestRole] = mapped_column(pg_enum(TestRole), nullable=False)
    status: Mapped[TestStatus] = mapped_column(pg_enum(TestStatus), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    message_excerpt: Mapped[str | None] = mapped_column(sa.String(2000))

    __table_args__ = (
        sa.Index("ix_test_results_task_run", "evaluation_task_run_id"),
        sa.Index("ix_test_results_task_run_role", "evaluation_task_run_id", "role"),
    )


__all__ = [
    "EvaluationRun",
    "EvaluationTaskRun",
    "PatchArtifact",
    "TestResult",
]
