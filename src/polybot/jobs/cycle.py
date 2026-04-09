from __future__ import annotations
import asyncio
import uuid
import structlog
from datetime import datetime, timezone
from ..config import BotConfig
from ..models import MarketSnapshot, PortfolioState, Trade, TradeStatus, utc_now
from ..market_discovery.fetcher import MarketFetcher
from ..market_discovery.filters import OperationalGuards
from ..feature_builder.builder import FeatureBuilder
from ..signal_engine.engine import SignalEngine
from ..risk_engine.sizer import RiskEngine
from ..execution_engine.paper import PaperExecutionEngine
from ..analytics.metrics import MetricsEngine
from ..analytics.validation import ValidationEngine
from ..strategy.default import DefaultStrategy
from ..scoring import InefficiencyScore, InefficiencyScorer
from .. import db as storage

logger = structlog.get_logger(__name__)


class BotCycle:
    """
    Main bot orchestration loop — score-based ranking edition.

    Each cycle runs in two phases:

    Phase 1 — Score all markets (per-market, independent):
      1.  Fetch active markets (MarketFetcher)
      2.  Apply operational guards (OperationalGuards)
      3.  Build features (FeatureBuilder)
      4.  Predict probability (InefficiencyModel via strategy)
      5.  Calibrate predictions
      6.  Compute signal (SignalEngine)         — saved to DB for all markets
      7.  Score market (InefficiencyScorer)     — transversal microstructure score
      8.  Filter: guard.passed AND score ≥ min_final_score → candidates list

    Phase 2 — Rank and execute top N:
      9.  Sort candidates by final_trade_score descending
      10. Take top top_n_per_cycle
      11. For each: check existing position → risk sizing → paper execution
      12. Check exits for open positions
      13. Persist funnel event summary
      14. Compute metrics

    Why two phases instead of one pass:
      - Ranking requires knowing all scores before selecting any.
      - Prevents "first come, first served" bias toward markets fetched first.
      - Keeps risk budget focused on the best opportunities each cycle.

    Funnel counters
    ---------------
      markets_fetched   — total markets returned by fetcher
      signals_computed  — markets for which a signal was computed and saved
      passed_guards     — subset where guard_result.passed = True
      scored_above_min  — guard-passed markets scoring ≥ min_final_score
      positive_edge_net — top N selected for risk evaluation (≤ scored_above_min)
      already_positioned— selected candidates skipped: position already open
      risk_approved     — sent to risk engine and approved
      risk_rejected     — sent to risk engine and rejected
      risk_trimmed      — approved but size was capped
      executed          — positions actually opened
      exited            — positions closed this cycle

    Invariant:
      already_positioned + risk_approved + risk_rejected = positive_edge_net
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.strategy = DefaultStrategy(config)
        self.fetcher = MarketFetcher()
        self.guards = OperationalGuards(config.guards)
        self.features = FeatureBuilder()
        self.signals = SignalEngine(config)
        self.scorer = InefficiencyScorer()
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

        logger.info(
            "cycle.start",
            cycle_id=cycle_id,
            mode="paper" if self.config.operation.paper_trade else "live",
        )

        await storage.ensure_schema(self.config.operation.db_path)

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

        # ── Funnel counters ───────────────────────────────────────────────────
        funnel: dict[str, int] = {
            "markets_fetched":    len(markets),
            "passed_guards":      0,
            "signals_computed":   0,
            "scored_above_min":   0,   # passed scoring gate (informational)
            "positive_edge_net":  0,   # top N actually sent to risk engine
            "already_positioned": 0,
            "risk_approved":      0,
            "risk_rejected":      0,
            "risk_trimmed":       0,
            "executed":           0,
            "exited":             0,
        }
        positions_opened = 0

        # Tracks why non-selected markets were skipped (diagnostic only).
        rejection_counts: dict[str, int] = {}

        # ── PHASE 1: score every market, collect candidates ───────────────────
        # candidates: list of (snapshot, signal, score) sorted later by score
        candidates: list[tuple[MarketSnapshot, object, InefficiencyScore]] = []

        for market in markets:
            try:
                await storage.save_market_snapshot(self.config.operation.db_path, market)

                guard  = self.guards.check(market)
                feats  = self.features.build(market)
                pred   = self.strategy.model.predict(feats)
                cal    = self.strategy.calibrator.calibrate(pred)
                sig    = self.signals.compute_signal(market, feats, cal, guard)

                await storage.save_signal(self.config.operation.db_path, sig)
                funnel["signals_computed"] += 1

                if guard.passed:
                    funnel["passed_guards"] += 1

                # Gate 1: guard must pass
                if not guard.passed:
                    reasons = [r.value for r in guard.failures]
                    key = f"guard_failed:{','.join(reasons) if reasons else 'unknown'}"
                    rejection_counts[key] = rejection_counts.get(key, 0) + 1
                    continue

                # Score ALL guard-passed markets — even those below min_final_score.
                # Saving score for non-selected markets lets cohort_report.py compare
                # the selected vs non-selected populations and measure scorer validity.
                score = self.scorer.score(market, feats)
                await storage.update_signal_score(
                    self.config.operation.db_path, sig.signal_id, score
                )

                if score.final_trade_score < self.config.scoring.min_final_score:
                    key = (
                        f"below_min_score("
                        f"score={score.final_trade_score:.3f}"
                        f"<min={self.config.scoring.min_final_score})"
                    )
                    rejection_counts[key] = rejection_counts.get(key, 0) + 1
                    continue

                funnel["scored_above_min"] += 1
                candidates.append((market, sig, score))

            except Exception as e:
                logger.error("cycle.market_error", market_id=market.market_id, error=str(e))
                continue

        # Per-cycle rejection summary — one INFO line with the full breakdown
        if rejection_counts:
            top = sorted(rejection_counts.items(), key=lambda x: x[1], reverse=True)
            logger.info(
                "cycle.signal_rejections",
                cycle_id=cycle_id,
                total_rejected=sum(rejection_counts.values()),
                breakdown={r: c for r, c in top},
            )

        # ── PHASE 2: rank by score, execute top N ─────────────────────────────
        candidates.sort(key=lambda x: x[2].final_trade_score, reverse=True)
        top_candidates = candidates[: self.config.scoring.top_n_per_cycle]
        funnel["positive_edge_net"] = len(top_candidates)

        if top_candidates:
            logger.info(
                "cycle.top_candidates",
                cycle_id=cycle_id,
                n_candidates=len(candidates),
                n_selected=len(top_candidates),
                top_score=top_candidates[0][2].final_trade_score,
                bottom_score=top_candidates[-1][2].final_trade_score,
            )

        for market, sig, score in top_candidates:
            try:
                # Skip if already holding a position in this market
                if any(t.market_id == market.market_id for t in self._portfolio.open_trades):
                    funnel["already_positioned"] += 1
                    continue

                # Risk sizing — always returns SizingDecision for funnel tracking
                decision = self.risk.size_position(sig, self._portfolio, today_str)
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
                        final_trade_score=score.final_trade_score,
                        inefficiency_score=score.inefficiency_score,
                        execution_score=score.execution_score,
                        suggested_side=score.suggested_side,
                    )

            except Exception as e:
                logger.error("cycle.market_error", market_id=market.market_id, error=str(e))
                continue

        # Step 12: Check exits for open positions
        for trade in list(self._portfolio.open_trades):
            try:
                current_price = await self._get_current_price(trade.market_id, markets)
                if current_price is None:
                    continue

                should_exit, reason = self.executor.should_exit(trade, current_price)
                if should_exit:
                    closed_trade = await self.executor.close_position(trade, current_price, reason)
                    await storage.update_trade_exit(
                        self.config.operation.db_path,
                        closed_trade.trade_id,
                        {
                            "exit_price":    closed_trade.exit_price,
                            "exit_time":     closed_trade.exit_time,
                            "exit_reason":   closed_trade.exit_reason,
                            "pnl_usd":       closed_trade.pnl_usd,
                            "pnl_pct":       closed_trade.pnl_pct,
                            "slippage_exit": closed_trade.slippage_exit,
                            "mae_usd":       closed_trade.mae_usd,
                            "mfe_usd":       closed_trade.mfe_usd,
                            "outcome":       closed_trade.outcome,
                            "status":        closed_trade.status,
                            "notes":         closed_trade.notes,
                            "updated_at":    closed_trade.updated_at,
                        },
                    )
                    self._portfolio.open_trades = [
                        t for t in self._portfolio.open_trades if t.trade_id != trade.trade_id
                    ]
                    self._portfolio.realized_pnl_usd += closed_trade.pnl_usd or 0
                    self._portfolio.daily_pnl_usd += closed_trade.pnl_usd or 0
                    funnel["exited"] += 1
            except Exception as e:
                logger.error("cycle.exit_error", trade_id=trade.trade_id, error=str(e))

        # Step 13: Persist funnel events
        await storage.save_funnel_events(
            self.config.operation.db_path,
            cycle_id=cycle_id,
            counts=funnel,
            created_at=cycle_start.isoformat(),
        )

        # Step 14: Recalibrate + metrics
        await self._maybe_recalibrate()
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

        logger.info(
            "bot.starting",
            paper_trade=self.config.operation.paper_trade,
            interval=interval,
        )

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
