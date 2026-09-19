"""把适配器的成本结果归一化为平台口径（E5-T5）。"""

from __future__ import annotations

from app.domain.cost import TokenPrices, estimate_cost_usd
from app.domain.enums import CostSource
from app.runner.protocol import AgentRunResult


def estimate_unavailable_cost(result: AgentRunResult, prices: TokenPrices | None) -> AgentRunResult:
    """仅为正常结束且 token 完整的 unavailable 结果补算成本。

    ``reported`` 是 Agent/服务商给出的事实，绝不能覆盖。带 ``error`` 的运行可能
    只有中途收到的部分 token，也不能拿已知部分冒充完整账单。
    """
    if result.cost_source is not CostSource.UNAVAILABLE:
        return result
    if result.error is not None or result.token_usage is None or prices is None:
        return result
    usage = result.token_usage
    amount = estimate_cost_usd(
        tokens_input=usage.input,
        tokens_output=usage.output,
        tokens_cache_read=usage.cache_read,
        prices=prices,
    )
    if amount is None:
        return result
    return result.model_copy(
        update={"cost_usd": float(amount), "cost_source": CostSource.ESTIMATED}
    )


__all__ = ["estimate_unavailable_cost"]
