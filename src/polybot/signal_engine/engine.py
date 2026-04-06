from __future__ import annotations
import uuid
import structlog
from ..models import (
    MarketSnapshot, MarketFeatures, CalibratedPrediction,
    Signal, GuardResult, Side, EntryReason
)
from ..config import BotConfig
from ..execution_engine.fill_model import FillModel

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
        self._fill_model = FillModel(config.costs)

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

        # Cost model — uses FillModel (half-spread is the dominant cost)
        fill = self._fill_model.estimate(snapshot, side)
        costs = fill.as_cost_estimate
        edge_net = edge_raw - fill.total_pct

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

    def is_actionable(self, signal: Signal) -> bool:
        if not signal.guard_result.passed:
            return False
        if signal.edge_net < self.config.model.min_edge_net:
            return False
        if abs(signal.edge_raw) < self.config.model.min_edge_raw:
            return False
        return True

    def rejection_reason(self, signal: Signal) -> str:
        """
        Returns the first failing check for a non-actionable signal.
        Used for diagnostics only — not part of the trading logic.
        """
        if not signal.guard_result.passed:
            reasons = [r.value for r in signal.guard_result.failures]
            return f"guard_failed:{','.join(reasons) if reasons else 'unknown'}"
        if signal.edge_net < self.config.model.min_edge_net:
            return (
                f"edge_net_below_min("
                f"edge_net={signal.edge_net:.4f} "
                f"< min={self.config.model.min_edge_net})"
            )
        if abs(signal.edge_raw) < self.config.model.min_edge_raw:
            return (
                f"edge_raw_below_min("
                f"abs_edge_raw={abs(signal.edge_raw):.4f} "
                f"< min={self.config.model.min_edge_raw})"
            )
        return "actionable"
