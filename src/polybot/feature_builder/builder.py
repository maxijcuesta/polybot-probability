"""
Feature builder: derive MarketFeatures from MarketSnapshot.

All computations are pure functions of the snapshot.
No I/O, no state. Returns None if data is insufficient.
"""
from __future__ import annotations

from ..models import MarketFeatures, MarketSnapshot


class FeatureBuilder:
    """
    Computes MarketFeatures from a MarketSnapshot.

    Stateless — can be reused across many markets.
    """

    def build(self, market: MarketSnapshot) -> MarketFeatures:
        """
        Extract all features from a market snapshot.

        Uses orderbook data when available, falls back to cruder estimates
        based on open_interest when orderbook is not present.
        """
        mid = market.mid
        spread_pct = market.spread_pct

        # ── Depth features ────────────────────────────────────────────────
        if market.orderbook is not None and (
            market.orderbook.bids or market.orderbook.asks
        ):
            bid_depth = market.orderbook.bid_depth_usd
            ask_depth = market.orderbook.ask_depth_usd
        else:
            # Rough fallback: split open interest equally between sides
            half_oi = market.open_interest / 2.0 if market.open_interest > 0 else 0.0
            bid_depth = half_oi
            ask_depth = half_oi

        total_depth = bid_depth + ask_depth
        depth_imbalance = (
            (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
        )

        # ── Volume / OI ratio ─────────────────────────────────────────────
        vol_oi_ratio = (
            market.volume_24h / market.open_interest
            if market.open_interest > 0
            else 0.0
        )

        # ── Time features ─────────────────────────────────────────────────
        hours_to_res = market.hours_to_resolution
        # Use large sentinel for unknown (model can learn to ignore)
        hours_to_res_val = hours_to_res if hours_to_res is not None else 9999.0

        hours_since = market.hours_since_last_trade
        hours_since_val = hours_since if hours_since is not None else 0.0

        return MarketFeatures(
            market_id=market.market_id,
            mid_price=mid,
            spread_pct=spread_pct,
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
            depth_imbalance=depth_imbalance,
            volume_24h=market.volume_24h,
            open_interest=market.open_interest,
            volume_oi_ratio=vol_oi_ratio,
            hours_to_resolution=hours_to_res_val,
            hours_since_last_trade=hours_since_val,
            is_binary=True,  # all Polymarket YES/NO markets are binary
        )

    def build_batch(self, markets: list[MarketSnapshot]) -> list[MarketFeatures]:
        """Build features for a list of markets."""
        return [self.build(m) for m in markets]
