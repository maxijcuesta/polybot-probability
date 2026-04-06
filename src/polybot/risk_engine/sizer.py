from __future__ import annotations
import structlog
from ..models import Signal, SizeResult, PortfolioState
from ..config import BotConfig

logger = structlog.get_logger(__name__)

class RiskEngine:
    """
    Position sizing via fractional Kelly criterion.

    Kelly fraction for binary market:
        f = edge / odds
        f_frac = kelly_fraction * f

    Constraints:
      - max_risk_per_trade_pct of bankroll
      - max_portfolio_exposure_pct total
      - daily loss limit
    """

    def __init__(self, config: BotConfig):
        self.config = config

    def size_position(
        self,
        signal: Signal,
        portfolio: PortfolioState,
    ) -> SizeResult | None:
        """
        Returns SizeResult or None if position should not be opened
        (risk limits exceeded, zero or negative size).
        """
        rc = self.config.risk

        # Check daily loss limit
        if portfolio.daily_pnl_usd < -rc.daily_loss_limit_usd:
            logger.warning("risk.daily_loss_limit_hit", daily_pnl=portfolio.daily_pnl_usd)
            return None

        # Check max drawdown
        if portfolio.current_drawdown_pct > rc.max_drawdown_pct:
            logger.warning("risk.max_drawdown_hit", drawdown_pct=portfolio.current_drawdown_pct)
            return None

        # Check concurrent positions
        if len(portfolio.open_trades) >= self.config.operation.max_concurrent_positions:
            logger.warning("risk.max_positions_hit", n_open=len(portfolio.open_trades))
            return None

        # Compute Kelly size
        edge = signal.edge_net
        if edge <= 0:
            return None

        if rc.use_kelly:
            # For binary market: Kelly f = edge / (1 - entry_price) where entry_price ~= p_market
            entry_price = signal.p_market if signal.side.value == "YES" else (1 - signal.p_market)
            odds = (1 - entry_price) / entry_price if entry_price < 1 else 1
            kelly_f = edge / (odds if odds > 0 else 1)
            kelly_f = min(kelly_f, 1.0)  # cap at 100%
            fraction = rc.kelly_fraction * kelly_f
        else:
            fraction = rc.max_risk_per_trade_pct / 100.0

        # Apply max risk per trade cap
        max_fraction = rc.max_risk_per_trade_pct / 100.0
        fraction = min(fraction, max_fraction)

        size_usd = portfolio.bankroll_usd * fraction

        # Check portfolio exposure
        available_exposure = (rc.max_portfolio_exposure_pct / 100.0) * portfolio.bankroll_usd
        remaining = available_exposure - portfolio.total_exposure_usd
        size_usd = min(size_usd, remaining)

        if size_usd < 1.0:
            logger.debug("risk.size_too_small", size_usd=size_usd)
            return None

        entry_price = signal.p_market if signal.side == signal.side.YES else (1.0 - signal.p_market)
        size_shares = size_usd / entry_price if entry_price > 0 else 0

        logger.info(
            "risk.position_sized",
            market_id=signal.market_id,
            size_usd=round(size_usd, 2),
            fraction=round(fraction, 4),
            kelly_f=round(fraction / rc.kelly_fraction if rc.use_kelly else fraction, 4),
        )

        return SizeResult(
            signal_id=signal.signal_id,
            size_usd=round(size_usd, 2),
            size_shares=round(size_shares, 4),
            entry_price=round(entry_price, 4),
            kelly_fraction_used=round(fraction, 4),
            reasoning=f"kelly={rc.use_kelly}, fraction={fraction:.4f}, edge_net={signal.edge_net:.4f}",
        )
