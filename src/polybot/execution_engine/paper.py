from __future__ import annotations
import uuid
import structlog
from datetime import datetime, timezone
from ..models import Signal, SizeResult, Trade, TradeStatus, ExitReason, Side, utc_now
from ..config import BotConfig

logger = structlog.get_logger(__name__)

class PaperExecutionEngine:
    """
    Paper trading engine. Simulates fills with slippage model.
    Records MAE/MFE across the life of each position.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        # Track price history per trade for MAE/MFE
        self._price_history: dict[str, list[float]] = {}

    async def open_position(self, signal: Signal, size: SizeResult) -> Trade:
        cc = self.config.costs
        # Simulate slippage on entry
        slippage = cc.slippage_model_pct
        if signal.side == Side.YES:
            # Buying YES: price moves up slightly
            fill_price = min(signal.p_market + slippage, 0.99)
        else:
            # Buying NO: equivalent fill
            fill_price = max(signal.p_market - slippage, 0.01)

        trade_id = str(uuid.uuid4())[:12]
        trade = Trade(
            trade_id=trade_id,
            market_id=signal.market_id,
            signal_id=signal.signal_id,
            side=signal.side,
            status=TradeStatus.OPEN,
            entry_price=round(fill_price, 6),
            entry_size_usd=size.size_usd,
            entry_shares=size.size_shares,
            entry_time=utc_now(),
            entry_reason=signal.entry_reason,
            p_model=signal.p_model,
            p_calibrated=signal.p_calibrated,
            p_market_entry=signal.p_market,
            edge_raw=signal.edge_raw,
            edge_net=signal.edge_net,
            slippage_entry=slippage,
        )
        self._price_history[trade_id] = [fill_price]

        logger.info(
            "paper.position_opened",
            trade_id=trade_id,
            market_id=signal.market_id,
            side=signal.side.value,
            fill_price=round(fill_price, 4),
            size_usd=size.size_usd,
        )
        return trade

    async def close_position(
        self,
        trade: Trade,
        current_price: float,
        reason: str,
    ) -> Trade:
        cc = self.config.costs
        slippage = cc.slippage_model_pct

        # Simulate exit slippage (adverse)
        if trade.side == Side.YES:
            fill_price = max(current_price - slippage, 0.01)
        else:
            fill_price = min(current_price + slippage, 0.99)

        # PnL calculation
        # YES side: we hold a YES token; profit when price rises
        #   pnl_per_share = fill_price - entry_price  (both are YES prices)
        #   cost_per_share = entry_price
        # NO side: we hold a NO token; profit when YES price falls
        #   pnl_per_share = entry_price - fill_price  (YES price fell = our gain)
        #   cost_per_share = 1.0 - entry_price  (NO token cost = 1 - YES price)
        if trade.side == Side.YES:
            pnl_per_share = fill_price - trade.entry_price
            cost_per_share = trade.entry_price
        else:
            pnl_per_share = trade.entry_price - fill_price
            cost_per_share = 1.0 - trade.entry_price

        pnl_usd = pnl_per_share * trade.entry_shares
        pnl_pct = pnl_per_share / cost_per_share if cost_per_share > 0 else 0

        # MAE/MFE
        history = self._price_history.get(trade.trade_id, [current_price])
        history.append(fill_price)
        if trade.side == Side.YES:
            price_changes = [p - trade.entry_price for p in history]
        else:
            price_changes = [trade.entry_price - p for p in history]
        mae = min(price_changes) * trade.entry_shares if price_changes else 0
        mfe = max(price_changes) * trade.entry_shares if price_changes else 0

        # Map reason string to ExitReason
        exit_reason_map = {r.value: r for r in ExitReason}
        exit_reason = exit_reason_map.get(reason, ExitReason.MANUAL)

        # Build closed trade (replace fields)
        closed = Trade(
            trade_id=trade.trade_id,
            market_id=trade.market_id,
            signal_id=trade.signal_id,
            side=trade.side,
            status=TradeStatus.CLOSED,
            entry_price=trade.entry_price,
            entry_size_usd=trade.entry_size_usd,
            entry_shares=trade.entry_shares,
            entry_time=trade.entry_time,
            entry_reason=trade.entry_reason,
            p_model=trade.p_model,
            p_calibrated=trade.p_calibrated,
            p_market_entry=trade.p_market_entry,
            edge_raw=trade.edge_raw,
            edge_net=trade.edge_net,
            exit_price=round(fill_price, 6),
            exit_time=utc_now(),
            exit_reason=exit_reason,
            pnl_usd=round(pnl_usd, 4),
            pnl_pct=round(pnl_pct, 6),
            slippage_entry=trade.slippage_entry,
            slippage_exit=slippage,
            mae_usd=round(mae, 4),
            mfe_usd=round(mfe, 4),
            outcome=trade.outcome,
            notes=trade.notes,
        )

        self._price_history.pop(trade.trade_id, None)

        logger.info(
            "paper.position_closed",
            trade_id=trade.trade_id,
            reason=reason,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=f"{pnl_pct*100:.2f}%",
        )
        return closed

    async def mark_to_market(
        self,
        trades: list[Trade],
        prices: dict[str, float],
    ) -> list[Trade]:
        """Update MAE/MFE tracking with current prices. Returns same list."""
        updated = []
        for trade in trades:
            if trade.market_id in prices:
                p = prices[trade.market_id]
                history = self._price_history.setdefault(trade.trade_id, [trade.entry_price])
                history.append(p)
            updated.append(trade)
        return updated

    def should_exit(self, trade: Trade, current_price: float) -> tuple[bool, str]:
        """Check exit conditions. Returns (should_exit, reason)."""
        ec = self.config.exit

        if trade.side == Side.YES:
            pnl_pct = (current_price - trade.entry_price) / trade.entry_price
        else:
            cost = 1.0 - trade.entry_price
            pnl_pct = (trade.entry_price - current_price) / cost if cost > 0 else 0

        if pnl_pct >= ec.take_profit_pct:
            return True, ExitReason.TAKE_PROFIT.value

        if pnl_pct <= -ec.stop_loss_pct:
            return True, ExitReason.STOP_LOSS.value

        if trade.entry_time:
            hours_held = (utc_now() - trade.entry_time).total_seconds() / 3600
            if hours_held >= ec.max_hold_hours:
                return True, ExitReason.MAX_HOLD.value

        if ec.trailing_stop_pct is not None:
            history = self._price_history.get(trade.trade_id, [])
            if history:
                if trade.side == Side.YES:
                    peak = max(history)
                    drawdown = (peak - current_price) / peak if peak > 0 else 0
                else:
                    peak = min(history)
                    drawdown = (current_price - peak) / peak if peak > 0 else 0
                if drawdown >= ec.trailing_stop_pct:
                    return True, ExitReason.TRAILING_STOP.value

        return False, ""
