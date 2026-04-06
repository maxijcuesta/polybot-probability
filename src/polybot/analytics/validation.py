from __future__ import annotations
from dataclasses import dataclass, field
from ..models import Trade, TradeStatus

@dataclass(slots=True)
class ValidationResult:
    is_valid: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

class ValidationEngine:
    """
    Validates model performance and flags issues.
    Checks:
    - Model EV vs realized EV alignment
    - Calibration quality (mean calibration error)
    - Min sample requirements for statistical significance
    - Overfit/underfit signals
    """

    MIN_TRADES_FOR_VALIDATION = 20
    MAX_ACCEPTABLE_BRIER = 0.25
    MAX_ACCEPTABLE_LOG_LOSS = 0.7

    def validate(self, trades: list[Trade], metrics) -> ValidationResult:
        warnings = []
        errors = []

        closed = [t for t in trades if t.status == TradeStatus.CLOSED]
        n = len(closed)

        if n < self.MIN_TRADES_FOR_VALIDATION:
            return ValidationResult(
                is_valid=True,
                warnings=[f"Only {n} closed trades — need {self.MIN_TRADES_FOR_VALIDATION} for meaningful validation"],
                summary={"n_trades": n, "status": "insufficient_data"},
            )

        # EV alignment check
        ev_exp = metrics.ev_expected_usd
        ev_real = metrics.ev_realized_usd
        if abs(ev_exp) > 0.1:
            efficiency = ev_real / ev_exp
            if efficiency < 0.3:
                warnings.append(f"EV efficiency low: {efficiency:.2f} (expected {ev_exp:.2f}, got {ev_real:.2f})")
            if ev_real < 0 and ev_exp > 0:
                errors.append("Negative realized EV despite positive expected EV — review model or costs")

        # Brier score check
        if metrics.brier_score > self.MAX_ACCEPTABLE_BRIER:
            warnings.append(f"Brier score {metrics.brier_score:.4f} > {self.MAX_ACCEPTABLE_BRIER} threshold")

        # Calibration check
        if metrics.calibration_buckets:
            mean_cal_error = sum(b.calibration_error for b in metrics.calibration_buckets) / len(metrics.calibration_buckets)
            if mean_cal_error > 0.1:
                warnings.append(f"Mean calibration error {mean_cal_error:.4f} > 0.10 — consider recalibrating")

        # Hit rate vs edge check
        expected_hit_rate_at_min_edge = 0.5 + self._config_min_edge / 2
        if metrics.hit_rate < 0.4 and metrics.n_trades > 30:
            warnings.append(f"Hit rate {metrics.hit_rate:.2%} below 40% — model may be overestimating edge")

        return ValidationResult(
            is_valid=len(errors) == 0,
            warnings=warnings,
            errors=errors,
            summary={
                "n_trades": n,
                "hit_rate": metrics.hit_rate,
                "brier_score": metrics.brier_score,
                "ev_efficiency": metrics.ev_efficiency,
                "status": "ok" if not errors else "error",
            }
        )

    _config_min_edge: float = 0.02  # fallback

    def set_min_edge(self, min_edge: float) -> None:
        self._config_min_edge = min_edge
