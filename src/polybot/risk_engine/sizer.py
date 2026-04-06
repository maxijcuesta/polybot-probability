from __future__ import annotations
import structlog
from ..models import (
    Signal, SizeResult, SizingDecision, PortfolioState,
    RejectReason, TrimReason,
)
from ..config import BotConfig

logger = structlog.get_logger(__name__)


class RiskEngine:
    """
    Sizing vía Kelly fraccionario.

    size_position() SIEMPRE devuelve SizingDecision — nunca None —
    para que el ciclo pueda persistir el motivo de rechazo/trim en cada señal.

    Jerarquía de caps (en orden de evaluación):
      1. daily_loss_limit / max_drawdown       → rechazo duro
      2. max_concurrent_positions              → rechazo duro
      3. daily_deployment_cap (agotado)        → rechazo duro
      4. zero / negative edge_net              → rechazo duro
      5. no_portfolio_room                     → rechazo duro
      6. event_cap (agotado)                   → rechazo duro
      7. below_min_size (después de caps)      → rechazo duro
      8. portfolio_exposure_cap (trim)         → aprobado con risk_limited
      9. daily_deployment_cap (trim parcial)   → aprobado con risk_limited
      10. event_cap (trim parcial)             → aprobado con risk_limited
      11. trade_cap (fraction > max_pct)       → aprobado con risk_limited
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        # Capital desplegado en posiciones nuevas hoy (reset automático por fecha)
        self._daily_deployed_usd: float = 0.0
        self._daily_reset_date: str = ""

    def reset_daily_counters(self, today_str: str) -> None:
        """Resetear contador diario cuando cambia la fecha."""
        if today_str != self._daily_reset_date:
            self._daily_deployed_usd = 0.0
            self._daily_reset_date = today_str

    # ─── API pública ──────────────────────────────────────────────────────────

    def size_position(
        self,
        signal: Signal,
        portfolio: PortfolioState,
        today_str: str = "",
    ) -> SizingDecision:
        """
        Calcula el tamaño de posición para una señal.

        Args:
            signal:     señal a evaluar
            portfolio:  estado actual del portfolio
            today_str:  "YYYY-MM-DD" — habilita tracking de cap diario

        Returns:
            SizingDecision con approved=True (ejecutar) o approved=False (no ejecutar).
            Siempre lleva motivo, tamaño solicitado, snapshot de caps y contexto Kelly.
        """
        rc = self.config.risk

        if today_str:
            self.reset_daily_counters(today_str)

        bankroll = portfolio.bankroll_usd
        daily_cap_usd = rc.max_daily_deployment_pct / 100.0 * bankroll
        daily_remaining = daily_cap_usd - self._daily_deployed_usd
        max_trade_cap_usd = rc.max_risk_per_trade_pct / 100.0 * bankroll

        # Snapshot de contexto (siempre útil para auditoría)
        _ctx = dict(
            bankroll_snapshot_usd=round(bankroll, 2),
            max_trade_cap_usd=round(max_trade_cap_usd, 2),
            daily_cap_remaining_usd=round(daily_remaining, 2),
        )

        def _rejected(reason: RejectReason, requested: float = 0.0, **extra) -> SizingDecision:
            return SizingDecision(
                approved=False,
                signal_id=signal.signal_id,
                reject_reason=reason,
                requested_size_usd=round(requested, 2),
                approved_size_usd=0.0,
                min_size_blocked=(reason == RejectReason.BELOW_MIN_SIZE),
                **_ctx,
                **extra,
            )

        # ── 1. Checks duros previos al sizing ────────────────────────────────

        if portfolio.daily_pnl_usd < -rc.daily_loss_limit_usd:
            logger.warning("risk.daily_loss_limit_hit", daily_pnl=portfolio.daily_pnl_usd)
            return _rejected(RejectReason.DAILY_LOSS_LIMIT)

        if portfolio.current_drawdown_pct > rc.max_drawdown_pct:
            logger.warning("risk.max_drawdown_hit", drawdown_pct=portfolio.current_drawdown_pct)
            return _rejected(RejectReason.MAX_DRAWDOWN)

        if len(portfolio.open_trades) >= self.config.operation.max_concurrent_positions:
            logger.warning("risk.max_positions_hit", n_open=len(portfolio.open_trades))
            return _rejected(RejectReason.MAX_CONCURRENT_POSITIONS)

        if daily_remaining <= 0:
            logger.warning(
                "risk.daily_cap_exhausted",
                deployed=self._daily_deployed_usd,
                cap=daily_cap_usd,
            )
            return _rejected(RejectReason.DAILY_CAP_EXHAUSTED)

        if signal.edge_net <= 0:
            return _rejected(RejectReason.NO_EDGE)

        # ── 2. Kelly / fracción fija ──────────────────────────────────────────

        edge = signal.edge_net
        if rc.use_kelly:
            entry_price = (
                signal.p_market if signal.side.value == "YES"
                else 1.0 - signal.p_market
            )
            entry_price = max(entry_price, 1e-6)
            odds = (1.0 - entry_price) / entry_price
            kelly_f_raw = min(edge / max(odds, 1e-6), 1.0)  # Kelly puro (sin fracción)
            fraction = rc.kelly_fraction * kelly_f_raw
        else:
            entry_price = (
                signal.p_market if signal.side.value == "YES"
                else 1.0 - signal.p_market
            )
            kelly_f_raw = None
            fraction = rc.max_risk_per_trade_pct / 100.0

        # Cap por trade (trade_cap): fracción máxima de bankroll por operación
        max_fraction = rc.max_risk_per_trade_pct / 100.0
        trim_reason: TrimReason | None = None
        risk_limited = False

        if fraction > max_fraction:
            fraction = max_fraction
            trim_reason = TrimReason.TRADE_CAP
            risk_limited = True

        kelly_f_applied = fraction
        requested_size_usd = bankroll * fraction  # tamaño Kelly antes de los demás caps

        # ── 3. Cap de exposición total del portfolio ──────────────────────────

        available_exposure = (rc.max_portfolio_exposure_pct / 100.0) * bankroll
        remaining_exposure = available_exposure - portfolio.total_exposure_usd

        if remaining_exposure <= 0:
            return _rejected(
                RejectReason.NO_PORTFOLIO_ROOM,
                requested=requested_size_usd,
                kelly_fraction_raw=round(kelly_f_raw, 6) if kelly_f_raw is not None else None,
                kelly_fraction_applied=round(kelly_f_applied, 6),
            )

        size_usd = requested_size_usd
        if size_usd > remaining_exposure:
            size_usd = remaining_exposure
            if trim_reason is None:
                trim_reason = TrimReason.PORTFOLIO_EXPOSURE_CAP
            risk_limited = True

        # ── 4. Cap diario (trim parcial) ─────────────────────────────────────

        if size_usd > daily_remaining:
            size_usd = daily_remaining
            if trim_reason is None:
                trim_reason = TrimReason.DAILY_CAP
            risk_limited = True

        # ── 5. Cap por evento ─────────────────────────────────────────────────

        event_cap_usd = rc.max_event_exposure_pct / 100.0 * bankroll
        current_event_exposure = sum(
            t.entry_size_usd
            for t in portfolio.open_trades
            if t.market_id == signal.market_id
        )
        event_remaining = event_cap_usd - current_event_exposure

        if event_remaining <= 0:
            logger.warning(
                "risk.event_cap_exhausted",
                market_id=signal.market_id,
                exposure=current_event_exposure,
                cap=event_cap_usd,
            )
            return _rejected(
                RejectReason.EVENT_CAP_EXHAUSTED,
                requested=requested_size_usd,
                kelly_fraction_raw=round(kelly_f_raw, 6) if kelly_f_raw is not None else None,
                kelly_fraction_applied=round(kelly_f_applied, 6),
                event_cap_remaining_usd=0.0,
            )

        if size_usd > event_remaining:
            size_usd = event_remaining
            if trim_reason is None:
                trim_reason = TrimReason.EVENT_CAP
            risk_limited = True

        # ── 6. Tamaño mínimo operativo ────────────────────────────────────────

        if size_usd < 1.0:
            return SizingDecision(
                approved=False,
                signal_id=signal.signal_id,
                reject_reason=RejectReason.BELOW_MIN_SIZE,
                min_size_blocked=True,
                requested_size_usd=round(requested_size_usd, 2),
                approved_size_usd=0.0,
                kelly_fraction_raw=round(kelly_f_raw, 6) if kelly_f_raw is not None else None,
                kelly_fraction_applied=round(kelly_f_applied, 6),
                **_ctx,
                event_cap_remaining_usd=round(event_remaining, 2),
            )

        # ── 7. Aprobado ───────────────────────────────────────────────────────

        self._daily_deployed_usd += size_usd
        size_shares = size_usd / max(entry_price, 1e-6)

        logger.info(
            "risk.approved",
            market_id=signal.market_id,
            requested_usd=round(requested_size_usd, 2),
            approved_usd=round(size_usd, 2),
            trim=trim_reason.value if trim_reason else None,
            kelly_raw=round(kelly_f_raw, 4) if kelly_f_raw is not None else None,
            kelly_applied=round(kelly_f_applied, 4),
            daily_deployed=round(self._daily_deployed_usd, 2),
            daily_cap=round(daily_cap_usd, 2),
        )

        return SizingDecision(
            approved=True,
            signal_id=signal.signal_id,
            requested_size_usd=round(requested_size_usd, 2),
            approved_size_usd=round(size_usd, 2),
            trim_reason=trim_reason,
            risk_limited=risk_limited,
            daily_cap_remaining_usd=round(daily_remaining - size_usd, 2),
            event_cap_remaining_usd=round(event_remaining - size_usd, 2),
            kelly_fraction_raw=round(kelly_f_raw, 6) if kelly_f_raw is not None else None,
            kelly_fraction_applied=round(kelly_f_applied, 6),
            bankroll_snapshot_usd=round(bankroll, 2),
            max_trade_cap_usd=round(max_trade_cap_usd, 2),
            size_shares=round(size_shares, 4),
            entry_price=round(entry_price, 4),
            reasoning=(
                f"kelly={rc.use_kelly}, f_applied={kelly_f_applied:.4f}, "
                f"edge_net={signal.edge_net:.4f}, trim={trim_reason}"
            ),
        )
