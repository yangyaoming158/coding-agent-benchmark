"""规则前置分类器（E6-T1，`06-judge-attribution.md` §12.1 / §12.2 第一步）。

一句话：**把失败里"不用想也能判死"的那部分判掉，剩下的才交给大模型。**

    RunFacts ──▶ classify() ──▶ RuleVerdict（判死了）
                            └─▶ RuleSkip（判不了，附原因）

## 为什么这一层最值钱

§12.2 说得很直接：F6、F7、F8、N1 这几类通常占失败的一半上下，而规则判定的
准确率接近 100%。它们直接把 MET-04（归因准确率 ≥85%）的底板抬起来，
比反复调提示词可靠得多。开工前在库里现有的 309 次失败上预演过一遍：
规则判死 238 次（77%），只有 71 次要大模型。

## `classify()` 是纯函数

没有时间、没有随机数、不碰文件系统、不连库、不连网。同一份 `RunFacts`
今天判和下个月判必须一样 —— 理由和判定引擎那条一模一样（`AGENTS.md` §5.1）。

## 责任方查表，不写 if

谁的锅这件事，协议 C-18 已经在 `INFRA_TO_AGENT_MAPPING` 里写死了，
C-19 明令禁止把它散进各处的 if 分支。所以这里**只查表**：

| 表里的字段 | 这里拿它判什么 |
|:---|:---|
| `counts_as_infra_failure = YES` | N1，平台的锅 |
| `counts_as_infra_failure = BY_CONTROL_RUN` | 判不了（见下） |
| `owner = AGENT` | F8，被测 AI 自己的工具/预算问题 |
| `owner = HUMAN` | 人工取消，根本不是失败，不归因 |

自己再抄一张责任方表出来，迟早和协议漂移，而漂移之后没有任何东西会报错。

## 六条规则，顺序不能换

```
1. RESOLVED                        → 不归因（它成功了）
2. infra_outcome 还没落            → 不归因（没跑完）
3. owner = HUMAN（CANCELLED）      → 不归因（人喊停的）
4. counts_as_infra_failure = YES   → N1   平台故障
5. agent_outcome 是空补丁/打不上   → F7
6. owner = AGENT                   → F8
7. F2P 全过但 P2P 挂了             → F6   回归
否则                               → 判不了，留给 E6-T2
```

两处顺序是有讲究的：

- **4 在 5、6 前面。** 平台自己坏了的时候，不许把账算到被测 AI 头上。
- **5 在 6 前面。** `PATCH_APPLY_FAILED` 的责任方也是 AGENT，但 §12.1 把
  "补丁打不上"划进 F7 不是 F8。先看 `agent_outcome` 才分得开这两类。

## `TEST_TIMEOUT` 判不了，不猜

协议 C-20 写死了：测试超时算谁的，必须先重跑、再跑一次不打补丁的对照组才知道。
对照组是 E4-T5，还没做。表里它那三个字段写的也确实是 `BY_CONTROL_RUN`。

猜错的代价是两个方向相反的错误：猜"AI 的锅"会冤枉 AI，猜"平台的锅"会放过死循环。
所以这里返回 `RuleSkip(NEEDS_CONTROL_RUN)`，一行都不写。

## 判不了的一行都不写

`failure_attributions` 上有 `UNIQUE(evaluation_task_run_id)`，一次运行只能有一行。
规则判不了时先落一行"猜的"，E6-T2 就得去覆盖它，而覆盖对不对没人验得了。
不写，位置留给大模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.enums import AgentOutcome, FailureCategory, InfraOutcome
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    FaultOwner,
    InfraFailureCounting,
)

#: `agent_outcome` 落在这两个值上，就是 §12.1 的 F7：没改代码、补丁打不上、
#: 或者只动了受保护的文件（后者会被过滤成空补丁，见协议 C-08b）。
_F7_OUTCOMES = frozenset({AgentOutcome.EMPTY_PATCH, AgentOutcome.INVALID_PATCH})


class RuleName(StrEnum):
    """哪条规则触发的。写进 evidence，方便抽检时顺着查回来。"""

    INFRA_FAILURE = "infra_failure"
    EMPTY_OR_INVALID_PATCH = "empty_or_invalid_patch"
    AGENT_FAULT = "agent_fault"
    REGRESSION = "regression"


class SkipReason(StrEnum):
    """规则为什么没给结论。这不是错误，是分流。"""

    #: 这次评测成功了，没有失败可归因。
    RESOLVED = "RESOLVED"
    #: 还没跑完（`infra_outcome` 还是空的）。
    NOT_FINISHED = "NOT_FINISHED"
    #: 人工取消的，不算失败。
    CANCELLED = "CANCELLED"
    #: `TEST_TIMEOUT`：要按协议 C-20 跑对照组才能定责任方（E4-T5）。
    NEEDS_CONTROL_RUN = "NEEDS_CONTROL_RUN"
    #: 规则分不出 F1~F5，交给 E6-T2 的大模型。
    NEEDS_LLM = "NEEDS_LLM"


@dataclass(frozen=True)
class RunFacts:
    """规则分类器要的全部输入。

    字段全部来自 `evaluation_task_runs` 这一行，**不需要读日志、不需要读补丁**。
    这样分类器才可能是纯函数，也才可能对全库几百次运行一次性回填。
    """

    #: 平台有没有正确完成这次评测。还没跑完时是 None。
    infra_outcome: InfraOutcome | None
    #: 被测 AI 有没有把 bug 修好。平台故障时按协议 C-18 可能是 None。
    agent_outcome: AgentOutcome | None
    #: F2P 过了几条 / 一共几条。没跑测试时是 None。
    f2p_passed: int | None = None
    f2p_total: int | None = None
    #: P2P 过了几条 / 一共几条。
    p2p_passed: int | None = None
    p2p_total: int | None = None
    #: AI 交上来的原始补丁是不是空的（过滤之前）。
    raw_patch_empty: bool | None = None
    #: 有没有试图改受保护的文件（`tests/`、`conftest.py` 之类）。
    protected_path_edit_attempted: bool | None = None


@dataclass(frozen=True)
class RuleVerdict:
    """规则给出的结论。`evidence` 里放触发这条规则时读到的字段原值。"""

    category: FailureCategory
    rule: RuleName
    evidence: dict[str, Any]


@dataclass(frozen=True)
class RuleSkip:
    """规则没给结论，以及为什么。"""

    reason: SkipReason


def classify(facts: RunFacts) -> RuleVerdict | RuleSkip:
    """按六条规则判一次失败，判不了就说判不了。

    纯函数：同样的 `facts` 永远给同样的结果。
    """
    if facts.agent_outcome is AgentOutcome.RESOLVED:
        return RuleSkip(SkipReason.RESOLVED)

    if facts.infra_outcome is None:
        return RuleSkip(SkipReason.NOT_FINISHED)

    rule = INFRA_TO_AGENT_MAPPING[facts.infra_outcome]

    # 人喊停的不是失败。放在平台故障判定前面：CANCELLED 本来就不计入平台故障率。
    if rule.owner is FaultOwner.HUMAN:
        return RuleSkip(SkipReason.CANCELLED)

    # TEST_TIMEOUT：协议 C-20 要跑对照组才知道算谁的，这里不猜。
    if rule.counts_as_infra_failure is InfraFailureCounting.BY_CONTROL_RUN:
        return RuleSkip(SkipReason.NEEDS_CONTROL_RUN)

    # 平台自己坏了，不许把账算到被测 AI 头上 —— 所以这一条在 F7 / F8 前面。
    if rule.counts_as_infra_failure is InfraFailureCounting.YES:
        return RuleVerdict(
            category=FailureCategory.N1_INFRASTRUCTURE_FAILURE,
            rule=RuleName.INFRA_FAILURE,
            evidence={
                "infra_outcome": facts.infra_outcome.value,
                "fault_owner": rule.owner.value,
                "counts_as_infra_failure": rule.counts_as_infra_failure.value,
            },
        )

    # F7 要排在 F8 前面：PATCH_APPLY_FAILED 的责任方也是 AGENT，
    # 但 §12.1 把"补丁打不上"划进 F7。
    if facts.agent_outcome in _F7_OUTCOMES:
        assert facts.agent_outcome is not None  # 上一行已经排除了 None
        return RuleVerdict(
            category=FailureCategory.F7_EMPTY_OR_INVALID_PATCH,
            rule=RuleName.EMPTY_OR_INVALID_PATCH,
            evidence={
                "agent_outcome": facts.agent_outcome.value,
                "raw_patch_empty": facts.raw_patch_empty,
                "protected_path_edit_attempted": facts.protected_path_edit_attempted,
                # 区分"真的一个字没改"和"改的全是受保护的文件"：
                # 后者是作弊未遂（协议 C-08b），抽检时要看得见。
                "empty_kind": _empty_kind(facts),
            },
        )

    if rule.owner is FaultOwner.AGENT:
        return RuleVerdict(
            category=FailureCategory.F8_AGENT_TOOL_OR_BUDGET_FAILURE,
            rule=RuleName.AGENT_FAULT,
            evidence={
                "infra_outcome": facts.infra_outcome.value,
                "fault_owner": rule.owner.value,
                "agent_outcome": _value_or_none(facts.agent_outcome),
            },
        )

    if _is_regression(facts):
        return RuleVerdict(
            category=FailureCategory.F6_REGRESSION,
            rule=RuleName.REGRESSION,
            evidence={
                "f2p": f"{facts.f2p_passed}/{facts.f2p_total}",
                "p2p": f"{facts.p2p_passed}/{facts.p2p_total}",
                "agent_outcome": _value_or_none(facts.agent_outcome),
            },
        )

    return RuleSkip(SkipReason.NEEDS_LLM)


def _is_regression(facts: RunFacts) -> bool:
    """F2P 全过、P2P 有挂的 —— 目标测试修好了，但把别的功能改坏了。

    两个 `total` 都要求大于 0：一条用例都没跑的时候，"全过"是句空话。
    """
    if not facts.f2p_total or not facts.p2p_total:
        return False
    if facts.f2p_passed is None or facts.p2p_passed is None:
        return False
    return facts.f2p_passed == facts.f2p_total and facts.p2p_passed < facts.p2p_total


def _empty_kind(facts: RunFacts) -> str:
    """空补丁是哪一种。协议 C-08b 要求这两种能分开。"""
    if facts.agent_outcome is AgentOutcome.INVALID_PATCH:
        return "patch_rejected"
    if facts.protected_path_edit_attempted:
        return "protected_paths_only"
    if facts.raw_patch_empty:
        return "nothing_changed"
    return "unknown"


def _value_or_none(outcome: AgentOutcome | None) -> str | None:
    return None if outcome is None else outcome.value


__all__ = [
    "RuleName",
    "RuleSkip",
    "RuleVerdict",
    "RunFacts",
    "SkipReason",
    "classify",
]
