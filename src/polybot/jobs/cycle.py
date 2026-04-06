from __future__ import annotations
import asyncio
import uuid
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
    1.  Fetch active markets (MarketFetcher)
    2.  Apply operational guards (OperationalGuards)
    3.  Build features (FeatureBuilder)
    4.  Predict probabilities (model)
    5.  Calibrate predictions (calibrator)
    6.  Compute signals (SignalEngine)
    7.  Size positions (RiskEngine)  — always returns SizingDecision for funnel tracking
    8.  Execute / update positions (ExecutionEngine)
    9.  Check exits for open positions
    10. Persist funnel event summary
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
        cycle_id = str(uuid.uuid4())[:12]
        cycle_start = utc_now()
        today_str = cycle_start.strftime("%Y-%m-%d")

        logger.info("cycle.start", cycle_id=cycle_id, mode="paper" if self.config.operation.paper_trade else "live")

        # Ensure DB schema (handles migrations automatically)
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

        # ── Contadores de funnel del ciclo ────────────────────────────────────
        # Definición exacta de cada etapa:
        #
        #  markets_fetched   — mercados devueltos por MarketFetcher.fetch_active_markets()
        #  passed_guards     — subset de signals_computed donde guard_result.passed = True
        #  signals_computed  — mercados para los que se calculó y persistió una señal
        #                      (≤ markets_fetched si algún mercado lanza excepción)
        #  positive_edge_net — señales is_actionable(): guard + edge_net ≥ min_edge_net
        #                      + edge_raw ≥ min_edge_raw
        #  already_positioned— señales accionables descartadas porque ese mercado ya
        #                      tiene una posición abierta en el portfolio (sin enviar
        #                      al risk engine; no cuentan como rejected)
        #  risk_approved     — señales enviadas al risk engine con SizingDecision.approved=True
        #  risk_rejected     — señales enviadas al risk engine con SizingDecision.approved=False
        #  risk_trimmed      — subset de risk_approved con SizingDecision.risk_limited=True
        #                      (tamaño aprobado < tamaño solicitado)
        #  executed          — posiciones abiertas efectivamente (paper o live)
        #  exited            — posiciones cerradas en este ciclo
        #
        # Invariante: risk_approved + risk_rejected + already_positioned
        #             = positive_edge_net  (modulo errores de excepción)
        funnel: dict[str, int] = {
            "markets_fetched":   len(markets),
            "passed_guards":     0,
            "signals_computed":  0,
            "positive_edge_net": 0,
            "already_positioned": 0,  # accionables omitidos por posición existente
            "risk_approved":     0,
            "risk_rejected":     0,
            "risk_trimmed":      0,
            "executed":          0,
            "exited":            0,
        }
        positions_opened = 0
        # Track why signals are not actionable, for per-cycle diagnostics.
        # Keys are rejection_reason() strings; values are counts.
        rejection_counts: dict[str, int] = {}

        # Step 2-8: Process each market
        for market in markets:
            try:
                # Persist snapshot
                await storage.save_market_snapshot(self.config.operation.db_path, market)

                # 2. Guards
                guard = self.guards.check(market)

                # 3. Features
                feats = self.features.build(market)

                # 4. Model
                prediction = self.strategy.model.predict(feats)

                # 5. Calibrate
                calibrated = self.strategy.calibrator.calibrate(prediction)

                # 6. Signal — computed for ALL markets regardless of guard outcome
                sig = self.signals.compute_signal(market, feats, calibrated, guard)
                await storage.save_signal(self.config.operation.db_path, sig)
                funnel["signals_computed"] += 1

                if guard.passed:
                    funnel["passed_guards"] += 1

                # Only actionable signals proceed: guard passed AND edge_net > 0
                if not self.signals.is_actionable(sig):
                    reason = self.signals.rejection_reason(sig)
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue
                funnel["positive_edge_net"] += 1

                # Descartar si ya hay posición abierta en este mercado.
                # Se cuenta por separado para no contaminar risk_rejected.
                if any(t.market_id == market.market_id for t in self._portfolio.open_trades):
                    funnel["already_positioned"] += 1
                    continue

                # 7. Size — always returns SizingDecision for full funnel tracking
                decision = self.risk.size_position(sig, self._portfolio, today_str)
                # Persist sizing outcome back to the signal row
                await storage.update_signal_sizing(
                    self.config.operation.db_path, sig.signal_id, decision
                )

                if not decision.approved:
                    funnel["risk_rejected"] += 1
                    logger.debug(
                        "risk.rejected",
                        market_id=market.market_id,
                        reason=decision.reject_reason.value if decision.reject_reason else None,
                        requested_usd=decision.requested_size_usd,
                    )
                    continue
                funnel["risk_approved"] += 1
                if decision.risk_limited:
                    funnel["risk_trimmed"] += 1

                # 8. Execute (paper mode)
                if self.config.operation.paper_trade:
                    size = decision.to_size_result()
                    trade = await self.executor.open_position(sig, size)
                    await storage.save_trade(self.config.operation.db_path, trade)
                    self._portfolio.open_trades.append(trade)
                    self._portfolio.total_exposure_usd += trade.entry_size_usd
                    positions_opened += 1
                    funnel["executed"] += 1
                    logger.info(
                        "cycle.position_opened",
                        market_id=market.market_id,
                        trade_id=trade.trade_id,
                        size_usd=size.size_usd,
                        approved_usd=decision.approved_size_usd,
                        requested_usd=decision.requested_size_usd,
                        trim_reason=decision.trim_reason,
                    )

            except Exception as e:
                logger.error("cycle.market_error", market_id=market.market_id, error=str(e))
                continue

        # Per-cycle rejection summary (diagnostic — emitted only when there are rejections)
        if rejection_counts:
            # Sort by count descending so the dominant reason appears first
            top = sorted(rejection_counts.items(), key=lambda x: x[1], reverse=True)
            logger.info(
                "cycle.signal_rejections",
                cycle_id=cycle_id,
                total_rejected=sum(rejection_counts.values()),
                breakdown={r: c for r, c in top},
            )

        # Step 9: Check exits for open positions
        for trade in list(self._portfolio.open_trades):
            try:
                current_price = await self._get_current_price(trade.market_id, markets)
                if current_price is None:
                    continue

                should_exit, reason = self.executor.should_exit(trade, current_price)
                if should_exit:
                    closed_trade = await self.executor.close_position(trade, current_price, reason)
                    await storage.update_trade_exit(self.config.operation.db_path, closed_trade)
                    self._portfolio.open_trades = [
                        t for t in self._portfolio.open_trades if t.trade_id != trade.trade_id
                    ]
                    self._portfolio.realized_pnl_usd += closed_trade.pnl_usd or 0
                    self._portfolio.daily_pnl_usd += closed_trade.pnl_usd or 0
                    funnel["exited"] += 1
            except Exception as e:
                logger.error("cycle.exit_error", trade_id=trade.trade_id, error=str(e))

        # Step 10: Persistir eventos de funnel (formato EAV — una fila por contador)
        await storage.save_funnel_events(
            self.config.operation.db_path,
            cycle_id=cycle_id,
            counts=funnel,
            created_at=cycle_start.isoformat(),
        )

        # Step 11: Recalibrate if enough data
        await self._maybe_recalibrate()

        # Step 12: Metrics
        all_trades = await storage.load_all_trades(self.config.operation.db_path, limit=500)
        metrics = self.metrics_engine.compute(all_trades)

        summary = {
            "status": "ok",
            "cycle_id": cycle_id,
            **funnel,
            "positions_opened": positions_opened,
            "open_positions": len(self._portfolio.open_trades),
            "bankroll_usd": self._portfolio.bankroll_usd,
            "daily_pnl_usd": round(self._portfolio.daily_pnl_usd, 2),
            "total_trades": metrics.n_trades,
            "hit_rate": metrics.hit_rate,
            "pnl_net": metrics.pnl_net_usd,
        }
        logger.info("cycle.complete", **{k: v for k, v in summary.items() if k != "status"})
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
