"""哨兵适配器共用的两样东西：补丁来源，和结果的组装（E3-T2）。

放在一起是因为 Oracle 和 Mock 都要"按 task_id 找一份现成的补丁"，
三个哨兵又都要按同一套规则填 `AgentRunResult` 里那十几个字段。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from app.domain.enums import CostSource
from app.runner.protocol import AgentError, AgentRunResult, TokenUsage

#: 哨兵适配器的版本号。它们没有外部依赖，版本只在协议报文变的时候才需要动。
SENTINEL_VERSION = "1.0"

#: 需要现成补丁的行为（Oracle 的全部工作、Mock 的"正确补丁"）查不到补丁时报的错误码。
#: 两个适配器共用一个码，报表里"补丁没喂进来"就只有一种写法。
GOLD_PATCH_MISSING = "gold_patch_missing"

#: 按 task_id 找补丁。找不到返回 None —— 返回空字符串是另一个意思（"这道题的补丁就是空的"）。
PatchLookup = Callable[[str], str | None]

#: 构造哨兵时能接受的补丁来源：一个现成的字典，或者一个自己去查的函数。
#: 字典够用在小数据集上；函数留给"几百道题、按需从制品库读"的场景。
PatchSource = Mapping[str, str] | PatchLookup


def as_lookup(source: PatchSource | None) -> PatchLookup:
    """把两种补丁来源都收敛成一个函数。`None` 收敛成"一道题都查不到"。"""
    if source is None:
        return lambda _task_id: None
    if callable(source):
        return source
    mapping = source
    return lambda task_id: mapping.get(task_id)


def sentinel_result(
    *,
    agent_name: str,
    started_at: datetime,
    patch: str = "",
    error: AgentError | None = None,
    exit_code: int = 0,
) -> AgentRunResult:
    """按同一套规则组装哨兵的结果。

    几个字段的取值都有理由，不是随手填的：

    - `model=None`：哨兵没调过任何模型。填上任务里那个模型名就是撒谎，
      报表会显示"这次评测用了 gpt-x"，而实际上一个 token 都没发出去。
    - `cost_source=reported` + `cost_usd=0.0`：成本确实是 0，而且我们知道它是 0。
      协议纪律 3 里"不要填 0"说的是**拿不到**成本时不要拿 0 顶替，
      和这里的"报得出来的 0"是两回事。
    - `token_usage` 全零：同上，是"确实没用"，不是"不知道用了多少"。
    - `patch_source="git_diff"`：这个字段区分的是"diff 由程序产出"还是
      "AI 自己在 stdout 里打印的"（后者行号常写错，归因时要能分开看）。
      哨兵的补丁要么来自上游真实的 git diff（Oracle），要么是程序拼的（Mock），
      两种都属于前者。
    """
    finished_at = datetime.now(UTC)
    return AgentRunResult(
        agent_name=agent_name,
        agent_version=SENTINEL_VERSION,
        model=None,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((finished_at - started_at).total_seconds() * 1000),
        exit_code=exit_code,
        patch=patch,
        patch_source="git_diff",
        token_usage=TokenUsage(),
        cost_usd=0.0,
        cost_source=CostSource.REPORTED,
        turns=0,
        error=error,
    )


__all__ = [
    "GOLD_PATCH_MISSING",
    "SENTINEL_VERSION",
    "PatchLookup",
    "PatchSource",
    "as_lookup",
    "sentinel_result",
]
