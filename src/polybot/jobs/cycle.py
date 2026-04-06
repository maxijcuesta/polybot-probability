from __future__ import annotations
import asyncio
import signal as signal_module
import structlog
from datetime import datetime, timezone
from ..config import BotConfig
from ..models import PortfolioState, Trade, TradeStatus, utc_now
from ..market_discovery.fetcher import MarketFetcher
from ..market_discovery.filters import OperationalGuards
from ..feature_builder.builder import FeatureBuilder
from ..signal_engine.engine import SignalEngine
from ..risk_engine.sizer import RiskEngine
from ..execution_engine.paper import PaperExecutionEngine
from ..analytics.metrics import MetricsEngine
from ..analytics.validation import ValidationEngine
from ..strategy.default import DefaultStrategy
from .. import db as storage

logger = structlog.get_logger(__name__)

class BotCycle:
    """
    Main bot orchestration loop.

    Each cycle:
    1. Fetch active markets (MarketFetcher)
    2. Apply operational guards (OperationalGuards)
    3. Build features (FeatureBuilder)
    4. Predict probabilities (model)
    5. Calibrate predictions (calibrator)
    6. Compute signals (SignalEngine)
    7. Size positions (RiskEngine)
    8. Execute / update positions (ExecutionEngine)
    9. Check exits for open positions
    10. Persist all data (db)
    11. Compute metrics (MetricsEngine)

    Runs in paper_trade mode by default.
    Set config.operation.live_trade=True for live execution.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.strategy = DefaultStrategy(config)
        self.fetcher = MarketFetcher()
        self.guards = OperationalGuards(config.guards)
        self.features = FeatureBuilder()
        self.signals = SignalEngine(config)
        self.risk = RiskEngine(config)
        self.executor = PaperExecutionEngine(config)
        self.metrics_engine = MetricsEngine()
        self.validator = ValidationEngine()
        self._running = False
        self._portfolio = PortfolioState(
            open_trades=[],
            realized_pnl_usd=0.0,
            unrealized_pnl_usd=0.0,
            total_exposure_usd=0.0,
            bankroll_usd=config.risk.bankroll_usd,
            daily_pnl_usd=0.0,
            peak_bankroll_usd=config.risk.bankroll_usd,
        )

    async def run_once(self) -> dict:
        """Run a single evaluation cycle. Returns summary dict."""
        logger.info("cycle.start", mode="paper" if self.config.operation.paper_trade else "live")

        # Ensure DB schema
        await storage.ensure_schema(self.config.operation.db_path)

        # Load open positions from DB
        open_trades = await storage.load_open_trades(self.config.operation.db_path)
        self._portfolio = PortfolioState(
            open_trades=open_trades,
            realized_pnl_usd=self._portfolio.realized_pnl_usd,
            unrealized_pnl_usd=0.0,
            total_exposure_usd=sum(t.entry_size_usd for t in open_trades),
            bankroll_usd=self._portfolio.bankroll_usd,
            daily_pnl_usd=self._portfolio.daily_pnl_usd,
            peak_bankroll_usd=self._portfolio.peak_bankroll_usd,
        )

        # Step 1: Fetch markets
        try:
            markets = await self.fetcher.fetch_active_markets(limit=200)
        except Exception as e:
            logger.error("cycle.fetch_failed", error=str(e))
            return {"status": "error", "error": str(e)}

        logger.info("cycle.markets_fetched", n=len(markets))

        # Step 2-8: Process each market
        signals_generated = 0
        positions_opened = 0

        for market in markets:
            try:
                # Save market snapshot
                await storage.save_market_snapshot(self.config.operation.db_path, market)

                # 2. Guards
                guard = self.guards.check(market)

                # 3. Features
                feats = self.features.build(market)

                # 4. Model
                prediction = self.strategy.model.predict(feats)

                # 5. Calibrate
                calibrated = self.strategy.calibrator.calibrate(prediction)

                # 6. Signal
                sig = self.signals.compute_signal(market, feats, calibrated, guard)
                await storage.save_signal(self.config.operation.db_path, sig)

                if not self.signals.is_actionable(sig):
                    continue
                signals_generated += 1

                # Skip if already have position in this market
                if any(t.market_id == market.market_id for t in self._portfolio.open_trades):
                    continue

                # 7. Size
                size = self.risk.size_position(sig, self._portfolio)
                if size is None:
                    continue

                # 8. Execute (paper mode)
                if self.config.operation.paper_trade:
                    trade = await self.executor.open_position(sig, size)
                    await storage.save_trade(self.config.operation.db_path, trade)
                    self._portfolio.open_trades.append(trade)
                    self._portfolio.total_exposure_usd += trade.entry_size_usd
                    positions_opened += 1
                    logger.info("cycle.position_opened", market_id=market.market_id, trade_id=trade.trade_id)

            except Exception as e:
                logger.error("cycle.market_error", market_id=market.market_id, error=str(e))
                continue

        # Step 9: Check exits for open positions
        exits = 0
        for trade in list(self._portfolio.open_trades):
            try:
                # Get current price for this market
                current_price = await self._get_current_price(trade.market_id, markets)
                if current_price is None:
                    continue

                should_exit, reason = self.executor.should_exit(trade, current_price)
                if should_exit:
                    closed_trade = await self.executor.close_position(trade, current_price, reason)
                    await storage.update_trade_exit(self.config.operation.db_path, closed_trade)
                    self._portfolio.open_trades = [t for t in self._portfolio.open_trades if t.trade_id != trade.trade_id]
                    self._portfolio.realized_pnl_usd += closed_trade.pnl_usd or 0
                    self._portfolio.daily_pnl_usd += closed_trade.pnl_usd or 0
                    exits += 1
            except Exception as e:
                logger.error("cycle.exit_error", trade_id=trade.trade_id, error=str(e))

        # Step 10: Recalibrate if enough data
        await self._maybe_recalibrate()

        # Step 11: Metrics
        all_trades = await storage.load_all_trades(self.config.operation.db_path, limit=500)
        metrics = self.metrics_engine.compute(all_trades)

        summary = {
            "status": "ok",
            "markets_scanned": len(markets),
            "signals_generated": signals_generated,
            "positions_opened": positions_opened,
            "positions_exited": exits,
            "open_positions": len(self._portfolio.open_trades),
            "bankroll_usd": self._portfolio.bankroll_usd,
            "daily_pnl_usd": round(self._portfolio.daily_pnl_usd, 2),
            "total_trades": metrics.n_trades,
            "hit_rate": metrics.hit_rate,
            "pnl_net": metrics.pnl_net_usd,
        }
        logger.info("cycle.complete", **summary)
        return summary

    async def _get_current_price(self, market_id: str, markets: list) -> float | None:
        for m in markets:
            if m.market_id == market_id:
                return m.mid
        return None

    async def _maybe_recalibrate(self) -> None:
        """Upgrade calibrator if we have enough resolved trades."""
        min_samples = self.config.model.calibration_min_samples
        all_trades = await storage.load_all_trades(self.config.operation.db_path, limit=500)
        resolved = [t for t in all_trades if t.outcome is not None and t.status == TradeStatus.CLOSED]
        if len(resolved) >= min_samples:
            p_predicted = [t.p_calibrated for t in resolved]
            outcomes = [t.outcome for t in resolved]
            self.strategy.upgrade_calibrator(p_predicted, outcomes)

    async def run_loop(self) -> None:
        """Run continuously until stopped."""
        self._running = True
        interval = self.config.operation.scan_interval_seconds

        logger.info("bot.starting", paper_trade=self.config.operation.paper_trade, interval=interval)

        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("cycle.unhandled_error", error=str(e))

            if self._running:
                logger.debug("cycle.sleeping", seconds=interval)
                await asyncio.sleep(interval)

        logger.info("bot.stopped")

    def stop(self) -> None:
        self._running = False
        logger.info("bot.stop_requested")
