"""平台统一 token→成本估算的算术。"""

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.cost import TokenPrices, estimate_cost_usd
from app.domain.enums import CostSource
from app.evaluation.costing import estimate_unavailable_cost
from app.runner.protocol import AgentError, AgentRunResult, TokenUsage


def prices() -> TokenPrices:
    return TokenPrices(
        input_per_mtok=Decimal("0.30"),
        output_per_mtok=Decimal("1.20"),
        cache_read_per_mtok=Decimal("0.006"),
    )


def test_cache_tokens_use_the_cache_price_not_the_input_price() -> None:
    amount = estimate_cost_usd(
        tokens_input=1_000_000,
        tokens_output=100_000,
        tokens_cache_read=900_000,
        prices=prices(),
    )

    # 10 万普通输入 + 90 万缓存输入 + 10 万输出。
    assert amount == Decimal("0.1554")


def test_no_cache_uses_normal_input_and_output_prices() -> None:
    amount = estimate_cost_usd(
        tokens_input=1_000_000,
        tokens_output=1_000_000,
        tokens_cache_read=0,
        prices=prices(),
    )

    assert amount == Decimal("1.50")


def test_estimate_matches_an_existing_reported_bill_sample() -> None:
    """历史实验 #125 的 22 次 Aider 实报为 $0.3453，估算误差应小于 10%。

    这不是新付费实验；token 与实报金额取自库中已有记录。它验证的是公式和真实
    账单在同一量级，避免只拿人为编出的整百万 token 自洽。
    """
    amount = estimate_cost_usd(
        tokens_input=1_294_500,
        tokens_output=25_471,
        tokens_cache_read=257_200,
        prices=TokenPrices(
            input_per_mtok=Decimal("0.27"),
            output_per_mtok=Decimal("1.10"),
            cache_read_per_mtok=Decimal("0.07"),
        ),
    )

    assert amount is not None
    reported = Decimal("0.345300")
    assert abs(amount - reported) / reported < Decimal("0.10")


def test_missing_price_or_token_keeps_cost_unknown() -> None:
    incomplete = TokenPrices(
        input_per_mtok=Decimal("0.30"),
        output_per_mtok=Decimal("1.20"),
    )
    assert (
        estimate_cost_usd(
            tokens_input=100,
            tokens_output=20,
            tokens_cache_read=80,
            prices=incomplete,
        )
        is None
    )
    assert (
        estimate_cost_usd(
            tokens_input=100,
            tokens_output=20,
            tokens_cache_read=None,
            prices=prices(),
        )
        is None
    )


def test_cache_read_cannot_exceed_input() -> None:
    assert (
        estimate_cost_usd(
            tokens_input=100,
            tokens_output=20,
            tokens_cache_read=101,
            prices=prices(),
        )
        is None
    )


def test_manifest_round_trip_preserves_decimal_text() -> None:
    original = prices()

    restored = TokenPrices.from_manifest(original.as_manifest())

    assert restored == original
    assert original.as_manifest()["cache_read_per_mtok"] == "0.006"


def result(
    *,
    source: CostSource = CostSource.UNAVAILABLE,
    cost: float | None = None,
    usage: TokenUsage | None = None,
    error: AgentError | None = None,
) -> AgentRunResult:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    return AgentRunResult(
        agent_name="test",
        started_at=now,
        finished_at=now,
        duration_ms=0,
        token_usage=usage,
        cost_usd=cost,
        cost_source=source,
        error=error,
    )


def test_platform_estimates_a_complete_unavailable_result() -> None:
    raw = result(usage=TokenUsage(input=1_000_000, output=100_000, cache_read=900_000))

    estimated = estimate_unavailable_cost(raw, prices())

    assert estimated.cost_usd == 0.1554
    assert estimated.cost_source is CostSource.ESTIMATED


def test_reported_cost_is_never_overwritten() -> None:
    reported = result(
        source=CostSource.REPORTED,
        cost=9.99,
        usage=TokenUsage(input=1_000_000, output=100_000, cache_read=900_000),
    )

    assert estimate_unavailable_cost(reported, prices()) is reported


def test_partial_or_missing_usage_stays_unavailable() -> None:
    interrupted = result(
        usage=TokenUsage(input=100, output=20, cache_read=0),
        error=AgentError(code="runtime_error", message="中途退出"),
    )

    assert estimate_unavailable_cost(interrupted, prices()).cost_source is CostSource.UNAVAILABLE
    assert estimate_unavailable_cost(result(), prices()).cost_source is CostSource.UNAVAILABLE
    assert (
        estimate_unavailable_cost(
            result(usage=TokenUsage(input=100, output=20, cache_read=0)), None
        ).cost_source
        is CostSource.UNAVAILABLE
    )
