"""评测协议的枚举定义。

**改动限制**：本文件第一部分（协议枚举）的取值由 `docs/evaluation-protocol.md`
（FROZEN v1.2）逐字规定。协议没改，这里就不能改；协议改了，这里必须同步改。
`tests/unit/test_enum_consistency.py` 会把两边对照一遍，不一致就让 CI 失败（协议 C-47）。

第二部分（平台枚举）来自 `docs/plan/07-platform-architecture.md` §13，
不受协议冻结约束，但改动同样要同步迁移脚本。

命名规则：成员名与取值完全一致（`RESOLVED = "RESOLVED"`），
少数取值在任务 JSON 里本来就是小写的（difficulty、issue_language、cost_source），
按 JSON 原样保留小写，避免导入题目时还要做一次大小写转换。
"""

from enum import StrEnum

# ══════════════════════════════════════════════════════════════
# 第一部分：协议枚举（FROZEN v1.2，改动需走协议变更流程）
# ══════════════════════════════════════════════════════════════


class LifecycleStatus(StrEnum):
    """一次评测走到哪一步了（协议 C-04）。

    终态只有三个：COMPLETED、FAILED、CANCELLED（协议 C-04a）。
    **没有 TIMEOUT 终态** —— 超时的具体类型记在 InfraOutcome 里。
    AI 自己超时算"拿到了结论"（COMPLETED），环境问题超时算"没拿到结论"（FAILED）。
    """

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    AGENT_RUNNING = "AGENT_RUNNING"
    PATCH_CAPTURED = "PATCH_CAPTURED"
    TESTING = "TESTING"
    JUDGING = "JUDGING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InfraOutcome(StrEnum):
    """这次跑得对不对 —— 平台自己有没有出故障（协议 C-05）。

    注意 OOM_KILLED 的判定方式：只能用 `docker inspect .State.OOMKilled`，
    **禁止**用退出码判断（协议 C-06、C-07）。内存超限和超时强杀的退出码都是 137，
    已在开发机实测确认，靠退出码会把两种相反的情况判成一样。
    """

    SUCCESS = "SUCCESS"
    ENV_BUILD_FAILED = "ENV_BUILD_FAILED"
    WORKSPACE_ERROR = "WORKSPACE_ERROR"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_RUNTIME_ERROR = "AGENT_RUNTIME_ERROR"
    AGENT_AUTH_ERROR = "AGENT_AUTH_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    OOM_KILLED = "OOM_KILLED"
    TEST_TIMEOUT = "TEST_TIMEOUT"
    TEST_DISCOVERY_ERROR = "TEST_DISCOVERY_ERROR"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    HARNESS_ERROR = "HARNESS_ERROR"
    CANCELLED = "CANCELLED"


class AgentOutcome(StrEnum):
    """被测 AI 修好了没有（协议 C-08）。

    非终态时该字段一律为 NULL（协议 C-09），所以数据库里它是可空的。

    EMPTY_PATCH 特别容易理解错：它的含义是"**标准化之后**的补丁为空"，
    不等于"AI 什么都没做"（协议 C-08a）。AI 可能改了一堆受保护路径下的文件
    想蒙混过关，被平台按 C-41 全部丢弃后也是空补丁。
    这两种行为要靠 raw_patch_empty / protected_path_edit_attempted 区分（C-08b）。
    """

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    EMPTY_PATCH = "EMPTY_PATCH"
    INVALID_PATCH = "INVALID_PATCH"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class TestStatus(StrEnum):
    """单条测试用例的状态（协议 C-10）。

    MISSING 表示题目里列了这条用例，但测试报告里找不到它（C-11）。
    **禁止**把 MISSING、SKIPPED、XFAIL 当作通过（C-12）。
    也**禁止**仅凭 MISSING 就判定作弊（C-13a）—— 用例 ID 归一化写错本身
    就会制造大量假 MISSING，这是本项目公认最容易出的静默 bug。
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    XFAIL = "XFAIL"
    XPASS = "XPASS"
    MISSING = "MISSING"


class EvaluationRunStatus(StrEnum):
    """一次实验的状态（协议 C-33）。

    COMPLETED：全部子任务结束，且平台故障题数 ≤ floor(总题数 × 5%) → 可进排行榜
    PARTIAL：  全部子任务结束，但平台故障题数超标 → 前端显示"降级"，不进排行榜
    FAILED：   调度层自己挂了

    界面上的"降级"只是 PARTIAL 的中文说法，**不要**再引入 DEGRADED 这个值（C-26b）。
    """

    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: 协议枚举登记表：条款号 → 枚举类。
#: 一致性测试遍历这张表逐个对照协议原文，新增协议枚举必须登记进来，
#: 否则它就不会被检查（协议 C-47）。
PROTOCOL_ENUMS: dict[str, type[StrEnum]] = {
    "C-04": LifecycleStatus,
    "C-05": InfraOutcome,
    "C-08": AgentOutcome,
    "C-10": TestStatus,
    "C-33": EvaluationRunStatus,
}

#: 终态集合（协议 C-04a）。只有终态允许有 agent_outcome。
TERMINAL_LIFECYCLE_STATUSES: frozenset[LifecycleStatus] = frozenset(
    {
        LifecycleStatus.COMPLETED,
        LifecycleStatus.FAILED,
        LifecycleStatus.CANCELLED,
    }
)

#: 非终态集合，等于全集减去终态。写成派生量而不是再抄一遍，避免两处不同步。
NON_TERMINAL_LIFECYCLE_STATUSES: frozenset[LifecycleStatus] = (
    frozenset(LifecycleStatus) - TERMINAL_LIFECYCLE_STATUSES
)


# ══════════════════════════════════════════════════════════════
# 第二部分：平台枚举（来源 docs/plan/07-platform-architecture.md §13）
# ══════════════════════════════════════════════════════════════


class ImageBuildStatus(StrEnum):
    """环境镜像的构建状态。镜像按仓库预建，是 6 小时跑完 300 次评测的前提（ADR-008）。"""

    PENDING = "PENDING"
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"


class TaskValidationState(StrEnum):
    """题目的验证状态。只有 VALID 的题目能进数据集。

    QUARANTINED 是隔离：题目复验也失败才会到这一步（协议 C-20 第 6 步）。
    **禁止**因为一次超时就直接隔离题目（协议 C-20a）。
    """

    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    VALIDATING = "VALIDATING"
    VALID = "VALID"
    INVALID = "INVALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    QUARANTINED = "QUARANTINED"


class BenchmarkSetStatus(StrEnum):
    """数据集版本的状态。PUBLISHED 之后题目清单就冻结了，靠 benchmark_set_items 快照保证。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class TaskCandidateState(StrEnum):
    """挖掘候选的状态。候选和正式题目分表存，避免污染 benchmark_tasks。"""

    DISCOVERED = "DISCOVERED"
    PRESCREENED = "PRESCREENED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class AgentKind(StrEnum):
    """被测 AI 的类型。

    MOCK / ORACLE / NOOP 是假 Agent，用来在完全不依赖外部服务的情况下测通整条链路：
    ORACLE 交官方补丁（解决率必须 100%），NOOP 交空补丁（必须 0%）。
    """

    MOCK = "MOCK"
    ORACLE = "ORACLE"
    NOOP = "NOOP"
    CLI = "CLI"
    CUSTOM = "CUSTOM"


class TaskDifficulty(StrEnum):
    """题目难度。由 gold_patch 改动行数、改动文件数、F2P 用例数三个客观量派生，不靠拍脑袋。

    取值用小写，与任务 JSON（`docs/plan/03-benchmark-spec.md` §7.1）保持一致。
    """

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class IssueLanguage(StrEnum):
    """Issue 正文的语言。zh 占比是本项目的公开指标之一，所以要单独存一列而不是靠事后检测。"""

    ZH = "zh"
    EN = "en"
    MIXED = "mixed"


class CostSource(StrEnum):
    """费用数字是怎么来的。

    unavailable 在订阅制 CLI 上很常见（它不报 token 用量），
    这时平台按 token_usage × 配置单价估算并标成 estimated，报告里必须区分显示。
    """

    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class PatchKind(StrEnum):
    """补丁的种类。

    AGENT_RAW 是 AI 交出来的原样，AGENT_NORMALIZED 是过滤受保护路径之后的。
    两份都要存：只存后者的话，"AI 试图改测试文件"这个行为就查不到了。
    """

    AGENT_RAW = "AGENT_RAW"
    AGENT_NORMALIZED = "AGENT_NORMALIZED"
    GOLD = "GOLD"
    TEST = "TEST"


class TestRole(StrEnum):
    """这条用例在题目里担任什么角色。

    F2P：修复前必须失败、修复后必须通过
    P2P：修复前后都必须通过，用来检查有没有把别的功能改坏
    OTHER：不在这两个名单里但报告中出现了的用例，存下来备查，不参与判定
    """

    F2P = "F2P"
    P2P = "P2P"
    OTHER = "OTHER"


class ArtifactOwnerType(StrEnum):
    """制品挂在谁名下。制品表用多态外键（owner_type + owner_id），不给每种拥有者建一张表。"""

    TASK = "TASK"
    TASK_RUN = "TASK_RUN"
    EVAL_RUN = "EVAL_RUN"
    VALIDATION = "VALIDATION"


class ArtifactKind(StrEnum):
    """制品的种类。日志、补丁、轨迹这些都可达数 MB，一律不入库，只在库里留索引行。"""

    AGENT_STDOUT = "AGENT_STDOUT"
    AGENT_STDERR = "AGENT_STDERR"
    TEST_STDOUT = "TEST_STDOUT"
    TEST_REPORT_XML = "TEST_REPORT_XML"
    TRAJECTORY = "TRAJECTORY"
    PATCH = "PATCH"
    REPORT_HTML = "REPORT_HTML"
    VALIDATION_EVIDENCE = "VALIDATION_EVIDENCE"
    BUILD_LOG = "BUILD_LOG"


class ArtifactBackend(StrEnum):
    """制品存在哪。切换只靠配置，业务代码零改动（ADR-005）。"""

    LOCAL = "LOCAL"
    MINIO = "MINIO"


class AttributionStage(StrEnum):
    """失败归因是哪一层给出的结论。规则层准确率接近 100%，大模型层只处理规则分不清的情况。"""

    RULE = "RULE"
    LLM = "LLM"
    HUMAN = "HUMAN"


class FailureCategory(StrEnum):
    """失败原因分类（`docs/plan/06-judge-attribution.md` §12.1）。

    F6、F7、F8、N1 靠纯规则就能判，不用调大模型；F1~F5 才需要大模型判断。
    N2 表示人工复核后确认**题目本身有问题** —— 它让抽检不只是纠正分类，
    还能反过来改进数据集质量：发现坏题 → 隔离 → 重算受影响的历史结果。
    """

    F1_REQUIREMENT_MISUNDERSTANDING = "F1_REQUIREMENT_MISUNDERSTANDING"
    F2_WRONG_FILE_LOCALIZATION = "F2_WRONG_FILE_LOCALIZATION"
    F3_INCOMPLETE_FIX = "F3_INCOMPLETE_FIX"
    F4_INCORRECT_LOGIC = "F4_INCORRECT_LOGIC"
    F5_SYNTAX_OR_BUILD_ERROR = "F5_SYNTAX_OR_BUILD_ERROR"
    F6_REGRESSION = "F6_REGRESSION"
    F7_EMPTY_OR_INVALID_PATCH = "F7_EMPTY_OR_INVALID_PATCH"
    F8_AGENT_TOOL_OR_BUDGET_FAILURE = "F8_AGENT_TOOL_OR_BUDGET_FAILURE"
    N1_INFRASTRUCTURE_FAILURE = "N1_INFRASTRUCTURE_FAILURE"
    N2_TASK_DEFECT = "N2_TASK_DEFECT"


class AttributionStatus(StrEnum):
    """归因任务本身的状态。NEEDS_HUMAN 表示自动归因给不出可信结论，要进人工复核队列。"""

    OK = "OK"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class HumanReviewAction(StrEnum):
    """人工复核的处理动作。

    MARK_TASK_DEFECT 对应 N2：复核人认为不是 AI 的问题，是题目坏了。
    """

    ACCEPT = "ACCEPT"
    CORRECT = "CORRECT"
    MARK_TASK_DEFECT = "MARK_TASK_DEFECT"
    COMMENT = "COMMENT"


class ReportScope(StrEnum):
    """报告覆盖范围：单次实验，还是多次实验横向对比。"""

    SINGLE_RUN = "SINGLE_RUN"
    COMPARISON = "COMPARISON"


class ReportFormat(StrEnum):
    """报告格式。"""

    HTML = "HTML"
    MARKDOWN = "MARKDOWN"
    JSON = "JSON"


class JobType(StrEnum):
    """作业类型。队列直接建在 Postgres 里，不引 Redis（ADR-003）。"""

    EVAL_TASK = "EVAL_TASK"
    VALIDATE_TASK = "VALIDATE_TASK"
    BUILD_IMAGE = "BUILD_IMAGE"
    ATTRIBUTE = "ATTRIBUTE"
    MINE_REPO = "MINE_REPO"
    GEN_REPORT = "GEN_REPORT"


class JobState(StrEnum):
    """作业状态。

    LEASED 是"已被某个 Worker 领走"。Worker 每 60 秒续一次租约；
    租约过期还没做完的会被回收，重置为 PENDING 或标成 DEAD。
    """

    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


#: 平台枚举登记表：数据库类型名 → 枚举类。
#: 迁移脚本靠它建原生枚举类型、回滚时靠它逐个 DROP TYPE。
#: 少了任何一个，`downgrade base` 之后再 `upgrade head` 会报 "type already exists"。
PLATFORM_ENUMS: dict[str, type[StrEnum]] = {
    "image_build_status": ImageBuildStatus,
    "task_validation_state": TaskValidationState,
    "benchmark_set_status": BenchmarkSetStatus,
    "task_candidate_state": TaskCandidateState,
    "agent_kind": AgentKind,
    "task_difficulty": TaskDifficulty,
    "issue_language": IssueLanguage,
    "cost_source": CostSource,
    "patch_kind": PatchKind,
    "test_role": TestRole,
    "artifact_owner_type": ArtifactOwnerType,
    "artifact_kind": ArtifactKind,
    "artifact_backend": ArtifactBackend,
    "attribution_stage": AttributionStage,
    "failure_category": FailureCategory,
    "attribution_status": AttributionStatus,
    "human_review_action": HumanReviewAction,
    "report_scope": ReportScope,
    "report_format": ReportFormat,
    "job_type": JobType,
    "job_state": JobState,
}

#: 全部枚举的数据库类型名 → 枚举类，供迁移脚本使用。
#: 协议枚举的类型名单独在这里指定，与条款号解耦（数据库里不该出现 "c-04" 这种名字）。
ALL_DB_ENUMS: dict[str, type[StrEnum]] = {
    "lifecycle_status": LifecycleStatus,
    "infra_outcome": InfraOutcome,
    "agent_outcome": AgentOutcome,
    "test_status": TestStatus,
    "evaluation_run_status": EvaluationRunStatus,
    **PLATFORM_ENUMS,
}
