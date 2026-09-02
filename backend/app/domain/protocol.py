"""协议里的常量表与判定约束。

这里放三样东西，都直接对应 `docs/evaluation-protocol.md`（FROZEN v1.2）：

1. **故障归属映射表** `INFRA_TO_AGENT_MAPPING`（C-18）
   协议 C-19 明确**禁止**把这张表的逻辑散落到各处的 if 分支里，所以它必须是
   一张显式的常量表，别处只许查表。

2. **合法组合表** `LEGAL_COMBINATIONS`（C-68、§4.3）
   `lifecycle_status × infra_outcome × agent_outcome` 一共 780 种组合，
   其中只有下面这些是合法的。表外的任何组合都是程序错误，必须在写库前拦下
   并抛异常，**禁止**静默落库（C-78）。

3. **门槛常量**：全局最大 attempt 数（C-71）、排行榜准入的平台故障率门槛（C-26）。

改动限制同 `enums.py`：协议没改，这里不能改。
`tests/unit/test_enum_consistency.py` 会把本文件与协议原文逐行对照。
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import (
    NON_TERMINAL_LIFECYCLE_STATUSES,
    AgentOutcome,
    InfraOutcome,
    LifecycleStatus,
)

#: 当前生效的协议版本号。实验创建时写进 evaluation_runs.protocol_version，
#: **禁止**事后修改（协议 C-67）。协议升版本时改这里。
PROTOCOL_VERSION = "v1.2"

#: 一道题的 attempt 总数上限（协议 C-71，建议值 4）。
#:
#: 为什么需要这个全局上限：C-18 是按错误类型分别规定重试次数的。一道题先遇到
#: ENV_BUILD_FAILED（重试 1 次）、再遇到 SANDBOX_ERROR（2 次）、再遇到 OOM_KILLED（1 次），
#: 每种错误各自的预算都没超，但这道题已经跑了 7 次。不设上限，重试预算会被
#: 不同错误类型轮流重置。
MAX_ATTEMPTS_PER_TASK = 4

#: 排行榜准入的平台故障率门槛（协议 C-26）。**大于**该值才不准入，正好等于可以进入。
INFRA_FAILURE_RATE_THRESHOLD = 0.05


def max_allowed_infra_failures(total_tasks: int) -> int:
    """一次实验最多允许几道题出平台故障，超过就不能进排行榜（协议 C-26a）。

    用向下取整的整数题数，不用浮点百分比 —— 避免"4.9999% 算不算超"这种争论。
    例：60 题最多允许 3 题，100 题最多允许 5 题。
    """
    return total_tasks * 5 // 100


# ══════════════════════════════════════════════════════════════
# 故障归属映射表（协议 C-18）
# ══════════════════════════════════════════════════════════════


class FaultOwner(StrEnum):
    """这个故障算谁的。决定它计不计入平台故障率，以及要不要罚被测 AI。"""

    NONE = "NONE"
    AGENT = "AGENT"
    PLATFORM = "PLATFORM"
    PLATFORM_OR_TASK = "PLATFORM_OR_TASK"
    EXTERNAL = "EXTERNAL"
    HUMAN = "HUMAN"
    BY_CONTROL_RUN = "BY_CONTROL_RUN"


class OutcomeRule(StrEnum):
    """怎么从 infra_outcome 得到 agent_outcome。

    有三条不是固定取值，必须再看别的证据才能定，所以单列出来而不是硬写一个值：
    BY_TEST_RESULT 要看逐条用例结果，BY_AGENT_STARTED 要看 agent_started_at，
    BY_CONTROL_RUN 要跑一次不打补丁的对照测试。
    """

    BY_TEST_RESULT = "BY_TEST_RESULT"
    BY_AGENT_STARTED = "BY_AGENT_STARTED"
    BY_CONTROL_RUN = "BY_CONTROL_RUN"
    FIXED_UNRESOLVED = "FIXED_UNRESOLVED"
    FIXED_INVALID_PATCH = "FIXED_INVALID_PATCH"
    FIXED_NOT_ATTEMPTED = "FIXED_NOT_ATTEMPTED"
    FIXED_NULL = "FIXED_NULL"


class InfraFailureCounting(StrEnum):
    """这次故障计不计入平台故障率。TEST_TIMEOUT 要跑完 C-20 的对照流程才知道。"""

    YES = "YES"
    NO = "NO"
    BY_CONTROL_RUN = "BY_CONTROL_RUN"


@dataclass(frozen=True)
class InfraFaultRule:
    """映射表的一行。字段顺序与协议 C-18 的表格列顺序一致，方便逐行对照。"""

    owner: FaultOwner
    outcome_rule: OutcomeRule
    counts_as_infra_failure: InfraFailureCounting
    max_auto_retries: int


#: 协议 C-18 的故障归属映射表。**查表，不要写 if**（C-19）。
INFRA_TO_AGENT_MAPPING: dict[InfraOutcome, InfraFaultRule] = {
    InfraOutcome.SUCCESS: InfraFaultRule(
        FaultOwner.NONE, OutcomeRule.BY_TEST_RESULT, InfraFailureCounting.NO, 0
    ),
    InfraOutcome.AGENT_TIMEOUT: InfraFaultRule(
        FaultOwner.AGENT, OutcomeRule.FIXED_UNRESOLVED, InfraFailureCounting.NO, 0
    ),
    InfraOutcome.AGENT_RUNTIME_ERROR: InfraFaultRule(
        FaultOwner.AGENT, OutcomeRule.FIXED_UNRESOLVED, InfraFailureCounting.NO, 1
    ),
    InfraOutcome.PATCH_APPLY_FAILED: InfraFaultRule(
        FaultOwner.AGENT, OutcomeRule.FIXED_INVALID_PATCH, InfraFailureCounting.NO, 0
    ),
    InfraOutcome.ENV_BUILD_FAILED: InfraFaultRule(
        FaultOwner.PLATFORM_OR_TASK, OutcomeRule.FIXED_NOT_ATTEMPTED, InfraFailureCounting.YES, 1
    ),
    InfraOutcome.WORKSPACE_ERROR: InfraFaultRule(
        FaultOwner.PLATFORM, OutcomeRule.FIXED_NOT_ATTEMPTED, InfraFailureCounting.YES, 1
    ),
    InfraOutcome.SANDBOX_ERROR: InfraFaultRule(
        FaultOwner.PLATFORM, OutcomeRule.BY_AGENT_STARTED, InfraFailureCounting.YES, 2
    ),
    InfraOutcome.OOM_KILLED: InfraFaultRule(
        FaultOwner.PLATFORM_OR_TASK, OutcomeRule.FIXED_NULL, InfraFailureCounting.YES, 1
    ),
    InfraOutcome.TEST_DISCOVERY_ERROR: InfraFaultRule(
        FaultOwner.PLATFORM_OR_TASK, OutcomeRule.FIXED_NULL, InfraFailureCounting.YES, 1
    ),
    InfraOutcome.HARNESS_ERROR: InfraFaultRule(
        FaultOwner.PLATFORM, OutcomeRule.BY_AGENT_STARTED, InfraFailureCounting.YES, 1
    ),
    InfraOutcome.AGENT_AUTH_ERROR: InfraFaultRule(
        FaultOwner.EXTERNAL, OutcomeRule.BY_AGENT_STARTED, InfraFailureCounting.YES, 3
    ),
    InfraOutcome.TEST_TIMEOUT: InfraFaultRule(
        FaultOwner.BY_CONTROL_RUN,
        OutcomeRule.BY_CONTROL_RUN,
        InfraFailureCounting.BY_CONTROL_RUN,
        1,
    ),
    InfraOutcome.CANCELLED: InfraFaultRule(
        FaultOwner.HUMAN, OutcomeRule.FIXED_NULL, InfraFailureCounting.NO, 0
    ),
}


def is_retryable(infra_outcome: InfraOutcome) -> bool:
    """这个故障允不允许自动重试。

    canonical attempt 的选取规则直接建在这个判断上（协议 C-24）：
    canonical = 第一个不可重试的结果；全都可重试时取重试耗尽后的最后一个 attempt。
    """
    return INFRA_TO_AGENT_MAPPING[infra_outcome].max_auto_retries > 0


# ══════════════════════════════════════════════════════════════
# 合法组合表（协议 C-68、§4.3）
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LegalCombination:
    """一种合法的三字段组合。

    `agent_outcome` 为 None 表示数据库里存 NULL。
    `condition` 是区分条件的中文说明 —— 同一个 (lifecycle, infra) 下有多个合法
    agent_outcome 时，靠它说明凭什么区分，直接抄自协议 §4.3 的表格。
    """

    lifecycle_status: LifecycleStatus
    infra_outcome: InfraOutcome
    agent_outcome: AgentOutcome | None
    condition: str
    #: 这一行要求被测 AI 启动过没有。True=必须启动过，False=必须没启动过，None=不限。
    #:
    #: 这不是新加的规定，是把协议 §4.3 表格里"区分条件"那一列的中文说明
    #: 翻译成机器能校验的形式。例如"只可能发生在 PREPARING，AI 必然未启动"
    #: 就是 False，"agent_started_at IS NOT NULL（跑起来后才 401）"就是 True。
    #: 数据库的 CHECK 约束直接用它，让 C-69 和 C-77 在写库这一层就被挡住。
    agent_started: bool | None


#: 协议 §4.3 "全部合法组合"表的终态部分（19 行）。
#: 非终态部分是一条通则（非终态一律 agent_outcome IS NULL），不逐行列举，
#: 见下面 `is_legal_combination` 的第一个分支。
LEGAL_COMBINATIONS: tuple[LegalCombination, ...] = (
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.SUCCESS,
        AgentOutcome.RESOLVED,
        "F2P 全过且 P2P 全过",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.SUCCESS,
        AgentOutcome.UNRESOLVED,
        "补丁非空但没同时满足两个条件",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.SUCCESS,
        AgentOutcome.EMPTY_PATCH,
        "AI 正常退出且过滤后补丁为空",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.AGENT_TIMEOUT,
        AgentOutcome.UNRESOLVED,
        "补丁可为空（C-08 例外）",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.AGENT_RUNTIME_ERROR,
        AgentOutcome.UNRESOLVED,
        "每个 attempt 各自定性，与是否还会重试无关",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.PATCH_APPLY_FAILED,
        AgentOutcome.INVALID_PATCH,
        "—",
        True,
    ),
    LegalCombination(
        LifecycleStatus.COMPLETED,
        InfraOutcome.TEST_TIMEOUT,
        AgentOutcome.UNRESOLVED,
        "对照组正常，只有打了补丁才超时 → AI 的问题（C-20 第 4 步）",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.AGENT_AUTH_ERROR,
        AgentOutcome.NOT_ATTEMPTED,
        "agent_started_at IS NULL（容器启动前就鉴权失败）",
        False,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.AGENT_AUTH_ERROR,
        None,
        "agent_started_at IS NOT NULL（跑起来后才 401）",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.ENV_BUILD_FAILED,
        AgentOutcome.NOT_ATTEMPTED,
        "只可能发生在 PREPARING，AI 必然未启动",
        False,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.WORKSPACE_ERROR,
        AgentOutcome.NOT_ATTEMPTED,
        "只可能发生在 PREPARING，AI 必然未启动",
        False,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.SANDBOX_ERROR,
        AgentOutcome.NOT_ATTEMPTED,
        "agent_started_at IS NULL（建 Agent 容器就失败）",
        False,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.SANDBOX_ERROR,
        None,
        "agent_started_at IS NOT NULL（建测试容器时失败）",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.OOM_KILLED,
        None,
        "只可能发生在 AGENT_RUNNING 或 TESTING，AI 必然已启动",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.TEST_TIMEOUT,
        None,
        "对照组也超时 → 环境问题（C-20 第 5 步）",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.TEST_DISCOVERY_ERROR,
        None,
        "只可能发生在 JUDGING，AI 必然已启动",
        True,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.HARNESS_ERROR,
        AgentOutcome.NOT_ATTEMPTED,
        "agent_started_at IS NULL",
        False,
    ),
    LegalCombination(
        LifecycleStatus.FAILED,
        InfraOutcome.HARNESS_ERROR,
        None,
        "agent_started_at IS NOT NULL",
        True,
    ),
    LegalCombination(
        LifecycleStatus.CANCELLED,
        InfraOutcome.CANCELLED,
        None,
        "—",
        None,
    ),
)

#: 查表用的索引：三字段组合 → 该组合要求 AI 启动过没有。省得每次线性扫一遍 19 行。
_LEGAL_TERMINAL: dict[tuple[LifecycleStatus, InfraOutcome, AgentOutcome | None], bool | None] = {
    (c.lifecycle_status, c.infra_outcome, c.agent_outcome): c.agent_started
    for c in LEGAL_COMBINATIONS
}


class IllegalCombinationError(ValueError):
    """三字段组合不在协议 §4.3 的合法组合表里。

    这不是数据问题，是程序 bug —— 说明某处的状态流转写错了。
    协议 C-78 要求在写库前抛出来，**禁止**静默落库。
    """


def is_legal_combination(
    lifecycle_status: LifecycleStatus,
    infra_outcome: InfraOutcome | None,
    agent_outcome: AgentOutcome | None,
    agent_started: bool | None = None,
) -> bool:
    """判断三字段组合合不合法（协议 C-68、§4.3、C-78）。

    `agent_started` 是调用方提供的事实：AI 到底启动过没有（即 `agent_started_at`
    是不是空的）。传 None 表示调用方没这个信息，那就只检查三字段本身。
    传了就一并检查 —— 有些组合只有在配上正确的启动状态时才合法，
    比如 `FAILED + ENV_BUILD_FAILED + NOT_ATTEMPTED` 要求 AI 必然没启动过。
    """
    if lifecycle_status in NON_TERMINAL_LIFECYCLE_STATUSES:
        # 非终态一律为空（协议 C-09）。infra_outcome 此时还没定，允许为 NULL。
        return agent_outcome is None
    if infra_outcome is None:
        # 终态必须已经知道这次跑得对不对。
        return False
    key = (lifecycle_status, infra_outcome, agent_outcome)
    if key not in _LEGAL_TERMINAL:
        return False
    required = _LEGAL_TERMINAL[key]
    if agent_started is None or required is None:
        return True
    return agent_started == required


def assert_legal_combination(
    lifecycle_status: LifecycleStatus,
    infra_outcome: InfraOutcome | None,
    agent_outcome: AgentOutcome | None,
    agent_started: bool | None = None,
) -> None:
    """写库前的组合校验，不合法就抛异常（协议 C-78）。"""
    if not is_legal_combination(lifecycle_status, infra_outcome, agent_outcome, agent_started):
        raise IllegalCombinationError(
            f"非法的三字段组合：lifecycle_status={lifecycle_status}, "
            f"infra_outcome={infra_outcome}, agent_outcome={agent_outcome}, "
            f"agent_started={agent_started}。"
            f"合法组合见 docs/evaluation-protocol.md §4.3。"
        )
