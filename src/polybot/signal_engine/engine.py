from __future__ import annotations
import uuid
import structlog
from ..models import (
    MarketSnapshot, MarketFeatures, CalibratedPrediction,
    Signal, CostEstimate, GuardResult, Side, EntryReason
)
from ..config import BotConfig

logger = structlog.get_logger(__name__)

class SignalEngine:
    """
    Computes trading signals from calibrated predictions.

    Signal anatomy:
      p_market = (best_bid + best_ask) / 2    # what the market implies
      p_calibrated = calibrated model output
      edge_raw = p_calibrated - p_market       # raw edge (before costs)
      costs = slippage + fees                  # transaction costs
      edge_net = edge_raw - costs.total        # net edge (what we capture)

    Only generates signal if:
      - guard_result.passed = True
      - edge_net > config.model.min_edge_net
      - side is determined (YES if p_cal > p_market, else NO)
    """

    def __init__(self, config: BotConfig):
        self.config = config

    def compute_signal(
        self,
        snapshot: MarketSnapshot,
        features: MarketFeatures,
        calibrated: CalibratedPrediction,
        guard_result: GuardResult,
    ) -> Signal:
        p_market = snapshot.mid
        p_cal = calibrated.p_yes_calibrated

        # Determine trading side
        # If model says YES is underpriced -> BUY YES
        # If model says NO is underpriced (p_yes is overpriced) -> BUY NO
        if p_cal > p_market:
            side = Side.YES
            edge_raw = p_cal - p_market
        else:
            side = Side.NO
            p_no_market = 1.0 - p_market
            p_no_model = 1.0 - p_cal
            edge_raw = p_no_model - p_no_market

        # Cost model
        costs = self._estimate_costs(snapshot, side)
        edge_net = edge_raw - costs.total

        signal = Signal(
            signal_id=str(uuid.uuid4())[:8],
            market_id=snapshot.market_id,
            side=side,
            p_market=p_market,
            p_model=calibrated.p_yes_raw,
            p_calibrated=p_cal,
            edge_raw=round(edge_raw, 6),
            costs=costs,
            edge_net=round(edge_net, 6),
            features=features,
            guard_result=guard_result,
            entry_reason=EntryReason.EDGE_THRESHOLD,
        )

        logger.debug(
            "signal.computed",
            market_id=snapshot.market_id,
            side=side.value,
            p_market=round(p_market, 4),
            p_calibrated=round(p_cal, 4),
            edge_raw=round(edge_raw, 4),
            edge_net=round(edge_net, 4),
            actionable=signal.is_actionable,
        )
        return signal

    def _estimate_costs(self, snapshot: MarketSnapshot, side: Side) -> CostEstimate:
        cc = self.config.costs
        # Taker: crossing spread
        spread_cost = snapshot.spread_pct * cc.slippage_model_pct
        slippage = cc.slippage_model_pct + spread_cost
        return CostEstimate(
            taker_fee=cc.taker_fee_pct,
            maker_fee=cc.maker_fee_pct,
            slippage=slippage,
            gas=cc.gas_cost_usd / max(self.config.risk.bankroll_usd * 0.02, 1),  # normalize
        )

    def is_actionable(self, signal: Signal) -> bool:
        if not signal.guard_result.passed:
            return False
        if signal.edge_net < self.config.model.min_edge_net:
            return False
        if abs(signal.edge_raw) < self.config.model.min_edge_raw:
            return False
        return True
