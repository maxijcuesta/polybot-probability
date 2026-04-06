"""
Fill model: transaction cost estimation for binary prediction markets.

The dominant cost in prediction markets is the half-spread — when you cross
the book you pay from mid to best_ask (for YES) or best_bid (for NO).

Cost hierarchy (descending importance):
  1. Half-spread  : spread / 2  → ~0.5–2% for typical Polymarket markets
  2. Market impact: grows with size relative to available depth
  3. Platform fee : 0% on Polymarket (currently)
  4. Gas          : tiny fixed ~$0.01 on Polygon

Critical implication:
  A signal needs edge_raw > half_spread to be viable BEFORE any model edge.
  At spread=0.02 → half_spread=0.01 → model must add >1% beyond market price.
  The naive model adds ≤3.5% maximum, so only large-adjustment signals survive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import CostEstimate, MarketSnapshot, Side, Orderbook
from ..config import CostConfig


@dataclass(slots=True)
class FillEstimate:
    """Detailed cost breakdown for a single fill."""
    # Fill mechanics
    side: str
    expected_fill_price: float    # where we expect to get filled
    mid_price: float
    half_spread: float            # DOMINANT COST: spread / 2

    # Secondary costs
    market_impact: float          # size-dependent slippage
    fee_pct: float                # platform fee (0% on Polymarket)
    gas_usd: float

    # Metadata
    depth_available_usd: float    # depth on the side we're hitting
    size_evaluated_usd: float     # size used for impact calc

    @property
    def total_pct(self) -> float:
        """Total cost as fraction of notional (excluding gas)."""
        return self.half_spread + self.market_impact + self.fee_pct

    @property
    def as_cost_estimate(self) -> CostEstimate:
        return CostEstimate(
            taker_fee=self.fee_pct,
            maker_fee=0.0,
            slippage=self.half_spread + self.market_impact,
            gas=self.gas_usd,
        )

    def survival_threshold(self) -> float:
        """Minimum raw edge needed to have positive expected net edge."""
        return self.total_pct


class FillModel:
    """
    Estimates expected transaction costs for a binary prediction market order.

    Usage:
        fm = FillModel(config.costs)
        estimate = fm.estimate(snapshot, Side.YES, size_usd=100)
        # estimate.total_pct is the breakeven edge_raw
        # signal.edge_raw must exceed estimate.total_pct for positive edge_net
    """

    # Market impact coefficient: cost = impact_coeff * (size / depth)
    # At 1% of depth → 0.001 cost. Conservative for small liquid markets.
    IMPACT_COEFF: float = 0.10
    # Maximum market impact cap (avoid absurd numbers for thin markets)
    MAX_IMPACT: float = 0.05  # 5%

    def __init__(self, config: CostConfig) -> None:
        self.config = config

    def estimate(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        size_usd: float = 100.0,
    ) -> FillEstimate:
        """
        Estimate fill costs for a given market, side, and size.

        Args:
            snapshot: current market state
            side: YES or NO
            size_usd: notional size in USD (used for impact calculation)
        """
        cc = self.config

        # ── Half-spread (dominant cost) ────────────────────────────────────────
        # Crossing the book: you fill at ask (YES) or bid (NO), paying the
        # half-spread vs the mid price.
        half_spread = snapshot.spread / 2.0  # in probability units = USD per share

        # ── Market impact ──────────────────────────────────────────────────────
        # Estimate available depth on the side we're hitting
        if snapshot.orderbook is not None:
            if side == Side.YES:
                depth = snapshot.orderbook.ask_depth_usd
            else:
                depth = snapshot.orderbook.bid_depth_usd
        else:
            # Fall back to OI estimate when no orderbook
            depth = snapshot.open_interest * 0.15  # ~15% of OI on each side
        depth = max(depth, 50.0)  # prevent div-by-zero for empty books

        impact = self.IMPACT_COEFF * (size_usd / depth)
        impact = min(impact, self.MAX_IMPACT)

        # ── Expected fill price ────────────────────────────────────────────────
        if side == Side.YES:
            fill_price = snapshot.best_ask + impact
        else:
            fill_price = snapshot.best_bid - impact
        fill_price = max(0.001, min(0.999, fill_price))

        # ── Gas (normalized to per-share basis) ───────────────────────────────
        gas_usd = cc.gas_cost_usd

        return FillEstimate(
            side=side.value,
            expected_fill_price=round(fill_price, 6),
            mid_price=snapshot.mid,
            half_spread=round(half_spread, 6),
            market_impact=round(impact, 6),
            fee_pct=cc.taker_fee_pct,
            gas_usd=gas_usd,
            depth_available_usd=round(depth, 2),
            size_evaluated_usd=size_usd,
        )

    def breakeven_edge(self, snapshot: MarketSnapshot, size_usd: float = 100.0) -> float:
        """
        Minimum edge_raw (per YES side) needed to have edge_net > 0.

        This is the spread-implied hurdle rate for any signal.
        If the model cannot reliably generate this much alpha, it's not tradeable.
        """
        yes_est = self.estimate(snapshot, Side.YES, size_usd)
        return yes_est.total_pct

    def effective_spread_cost(self, snapshot: MarketSnapshot) -> float:
        """
        The round-trip cost (entry + exit) in probability points.
        Even if exit is a limit order (maker), you still pay half-spread on entry.
        """
        entry_cost = self.estimate(snapshot, Side.YES).half_spread
        # Worst case: taker on both sides
        exit_cost = entry_cost
        return round(entry_cost + exit_cost, 6)
