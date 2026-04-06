"""
Operational guards: pre-flight checks before entering a position.

All 8 guards must pass for a market to be considered tradeable.
Guards are fast, stateless checks on MarketSnapshot data.
"""
from __future__ import annotations

from ..config import GuardsConfig
from ..models import GuardFailReason, GuardResult, MarketSnapshot


class OperationalGuards:
    """
    Runs all operational guards on a MarketSnapshot.

    Guards check:
    1. Minimum 24h volume
    2. Minimum open interest
    3. Maximum spread (liquidity guard)
    4. Minimum orderbook depth (both sides)
    5. Stale market protection (hours since last trade)
    6. Valid price range (avoid 0 and 1 edge cases)
    7. Minimum time to resolution (avoid last-minute markets)
    8. Maximum time to resolution (avoid very long-dated markets)
    + 9. Market must be active
    """

    def __init__(self, config: GuardsConfig) -> None:
        self._cfg = config

    def check(self, market: MarketSnapshot) -> GuardResult:
        """
        Run all guards on a market.

        Returns GuardResult with passed=True only if ALL guards pass.
        details dict contains the measured values for debugging.
        """
        failures: list[GuardFailReason] = []
        details: dict = {}

        # ── 1. Minimum 24h volume ──────────────────────────────────────────
        details["volume_24h"] = market.volume_24h
        if market.volume_24h < self._cfg.min_volume_24h_usd:
            failures.append(GuardFailReason.LOW_VOLUME)

        # ── 2. Minimum open interest ───────────────────────────────────────
        details["open_interest"] = market.open_interest
        if market.open_interest < self._cfg.min_open_interest_usd:
            failures.append(GuardFailReason.LOW_OPEN_INTEREST)

        # ── 3. Maximum spread ─────────────────────────────────────────────
        spread_pct = market.spread_pct
        details["spread_pct"] = spread_pct
        if spread_pct > self._cfg.max_spread_pct:
            failures.append(GuardFailReason.HIGH_SPREAD)

        # ── 4. Minimum orderbook depth ────────────────────────────────────
        if market.orderbook is not None:
            bid_depth = market.orderbook.bid_depth_usd
            ask_depth = market.orderbook.ask_depth_usd
        else:
            # Fallback: estimate depth from open interest (rough proxy)
            bid_depth = market.open_interest / 2
            ask_depth = market.open_interest / 2

        details["bid_depth_usd"] = bid_depth
        details["ask_depth_usd"] = ask_depth

        if bid_depth < self._cfg.min_depth_usd or ask_depth < self._cfg.min_depth_usd:
            failures.append(GuardFailReason.LOW_DEPTH)

        # ── 5. Stale market protection ────────────────────────────────────
        hours_since = market.hours_since_last_trade
        details["hours_since_last_trade"] = hours_since

        if hours_since is not None and hours_since > self._cfg.max_hours_since_last_trade:
            failures.append(GuardFailReason.STALE_MARKET)
        elif hours_since is None:
            # No last trade time available — treat as potentially stale
            # but don't fail outright; log it
            details["stale_warning"] = "no_last_trade_time"

        # ── 6. Valid price range ──────────────────────────────────────────
        mid = market.mid
        details["mid_price"] = mid
        if mid < self._cfg.min_yes_price or mid > self._cfg.max_yes_price:
            failures.append(GuardFailReason.INVALID_PRICE)

        # ── 7. Minimum time to resolution ────────────────────────────────
        hours_to_res = market.hours_to_resolution
        details["hours_to_resolution"] = hours_to_res

        if hours_to_res is not None and hours_to_res < self._cfg.min_time_to_resolution_hours:
            failures.append(GuardFailReason.TOO_CLOSE_RESOLUTION)

        # ── 8. Maximum time to resolution ────────────────────────────────
        max_hours = self._cfg.max_time_to_resolution_days * 24
        if hours_to_res is not None and hours_to_res > max_hours:
            failures.append(GuardFailReason.TOO_FAR_RESOLUTION)

        # ── 9. Market must be active ──────────────────────────────────────
        details["active"] = market.active
        if not market.active:
            # Map to closest guard reason — use STALE_MARKET as catch-all
            failures.append(GuardFailReason.STALE_MARKET)

        return GuardResult(
            passed=len(failures) == 0,
            failures=failures,
            details=details,
        )

    def check_batch(self, markets: list[MarketSnapshot]) -> list[tuple[MarketSnapshot, GuardResult]]:
        """Run guards on a list of markets. Returns (market, result) pairs."""
        return [(m, self.check(m)) for m in markets]

    def filter_passing(self, markets: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """Return only markets that pass all guards."""
        return [m for m in markets if self.check(m).passed]
