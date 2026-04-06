from __future__ import annotations
import math
from collections import defaultdict
from ..models import Trade, PerformanceMetrics, CalibrationBucket, TradeStatus, Side, utc_now

class MetricsEngine:
    """
    Computes all performance metrics from trade history.

    Metrics computed:
    - hit_rate: wins / total
    - pnl_gross/net
    - EV expected vs realized
    - Brier score: mean((p_predicted - outcome)^2)
    - Log loss: -mean(y*log(p) + (1-y)*log(1-p))
    - Calibration buckets (10 buckets, 0.0-0.1, ..., 0.9-1.0)
    - Segmentation: by side, by edge bucket, by market
    """

    N_CALIBRATION_BUCKETS = 10

    def compute(self, trades: list[Trade]) -> PerformanceMetrics:
        closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.pnl_usd is not None]

        n_trades = len(closed)
        if n_trades == 0:
            return self._empty_metrics()

        # Basic stats
        wins = [t for t in closed if (t.pnl_usd or 0) > 0]
        losses = [t for t in closed if (t.pnl_usd or 0) <= 0]
        n_wins = len(wins)
        n_losses = len(losses)
        hit_rate = n_wins / n_trades

        # PnL
        pnl_gross = sum(t.pnl_usd or 0 for t in closed)
        # Net PnL: gross minus fees (estimated from slippage_entry + slippage_exit)
        fees = sum((t.slippage_entry + t.slippage_exit) * (t.entry_size_usd or 0) for t in closed)
        pnl_net = pnl_gross - fees
        avg_pnl = pnl_gross / n_trades

        # Expected vs realized EV
        ev_expected = sum((t.edge_net or 0) * (t.entry_size_usd or 0) for t in closed)
        ev_realized = pnl_gross
        ev_efficiency = ev_realized / ev_expected if abs(ev_expected) > 0.001 else 0.0

        # Probabilistic metrics (only for resolved trades)
        resolved = [t for t in closed if t.outcome is not None]
        brier = self._brier_score(resolved)
        ll = self._log_loss(resolved)

        # Calibration buckets
        cal_buckets = self._calibration_buckets(resolved)

        # Segmentation
        by_side = self._segment_by(closed, lambda t: t.side.value)
        by_edge = self._segment_by_edge(closed)
        by_market = self._segment_by(closed, lambda t: t.market_id[:20])  # truncate for display

        # Risk
        max_dd = self._max_drawdown_usd(closed)
        gross_profit = sum(t.pnl_usd or 0 for t in wins)
        gross_loss = abs(sum(t.pnl_usd or 0 for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        avg_hold = self._avg_hold_hours(closed)

        return PerformanceMetrics(
            n_trades=n_trades,
            n_wins=n_wins,
            n_losses=n_losses,
            hit_rate=round(hit_rate, 4),
            pnl_gross_usd=round(pnl_gross, 2),
            pnl_net_usd=round(pnl_net, 2),
            avg_pnl_per_trade=round(avg_pnl, 4),
            ev_expected_usd=round(ev_expected, 4),
            ev_realized_usd=round(ev_realized, 4),
            ev_efficiency=round(ev_efficiency, 4),
            brier_score=round(brier, 6),
            log_loss=round(ll, 6),
            calibration_buckets=cal_buckets,
            by_side=by_side,
            by_edge_bucket=by_edge,
            by_market=by_market,
            max_drawdown_usd=round(max_dd, 2),
            sharpe_ratio=self._sharpe(closed),
            profit_factor=round(profit_factor, 4),
            avg_hold_hours=round(avg_hold, 2),
        )

    def _brier_score(self, trades: list[Trade]) -> float:
        if not trades:
            return 0.0
        total = sum((t.p_calibrated - (t.outcome or 0)) ** 2 for t in trades)
        return total / len(trades)

    def _log_loss(self, trades: list[Trade]) -> float:
        if not trades:
            return 0.0
        eps = 1e-7
        total = 0.0
        for t in trades:
            p = max(eps, min(1 - eps, t.p_calibrated))
            y = t.outcome or 0
            total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        return total / len(trades)

    def _calibration_buckets(self, trades: list[Trade]) -> list[CalibrationBucket]:
        if not trades:
            return []
        buckets: dict[int, list[Trade]] = defaultdict(list)
        for t in trades:
            bucket_idx = min(int(t.p_calibrated * self.N_CALIBRATION_BUCKETS), self.N_CALIBRATION_BUCKETS - 1)
            buckets[bucket_idx].append(t)

        result = []
        for i in range(self.N_CALIBRATION_BUCKETS):
            p_min = i / self.N_CALIBRATION_BUCKETS
            p_max = (i + 1) / self.N_CALIBRATION_BUCKETS
            bucket_trades = buckets.get(i, [])
            if not bucket_trades:
                continue
            avg_p = sum(t.p_calibrated for t in bucket_trades) / len(bucket_trades)
            win_rate = sum(1 for t in bucket_trades if (t.outcome or 0) == 1) / len(bucket_trades)
            cal_error = abs(avg_p - win_rate)
            result.append(CalibrationBucket(
                bucket_label=f"{p_min:.1f}-{p_max:.1f}",
                p_min=p_min,
                p_max=p_max,
                n_trades=len(bucket_trades),
                avg_p_model=round(avg_p, 4),
                observed_win_rate=round(win_rate, 4),
                calibration_error=round(cal_error, 4),
            ))
        return result

    def _segment_by(self, trades: list[Trade], key_fn) -> dict[str, dict]:
        groups: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            groups[key_fn(t)].append(t)
        return {
            k: {
                "n": len(v),
                "pnl": round(sum(t.pnl_usd or 0 for t in v), 2),
                "hit_rate": round(sum(1 for t in v if (t.pnl_usd or 0) > 0) / len(v), 4),
            }
            for k, v in groups.items()
        }

    def _segment_by_edge(self, trades: list[Trade]) -> dict[str, dict]:
        buckets = [
            ("0.00-0.02", 0.00, 0.02),
            ("0.02-0.05", 0.02, 0.05),
            ("0.05-0.10", 0.05, 0.10),
            ("0.10+",     0.10, 1.00),
        ]
        result = {}
        for label, lo, hi in buckets:
            group = [t for t in trades if lo <= abs(t.edge_net or 0) < hi]
            if not group:
                continue
            result[label] = {
                "n": len(group),
                "pnl": round(sum(t.pnl_usd or 0 for t in group), 2),
                "hit_rate": round(sum(1 for t in group if (t.pnl_usd or 0) > 0) / len(group), 4),
                "avg_edge": round(sum(abs(t.edge_net or 0) for t in group) / len(group), 4),
            }
        return result

    def _max_drawdown_usd(self, trades: list[Trade]) -> float:
        if not trades:
            return 0.0
        pnls = [t.pnl_usd or 0 for t in sorted(trades, key=lambda t: t.entry_time)]
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        return max_dd

    def _sharpe(self, trades: list[Trade]) -> float | None:
        if len(trades) < 5:
            return None
        returns = [t.pnl_pct or 0 for t in trades]
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = variance ** 0.5
        if std < 1e-9:
            return None
        return round(mean / std * (252 ** 0.5), 4)

    def _avg_hold_hours(self, trades: list[Trade]) -> float:
        times = []
        for t in trades:
            if t.exit_time and t.entry_time:
                hours = (t.exit_time - t.entry_time).total_seconds() / 3600
                times.append(hours)
        return sum(times) / len(times) if times else 0.0

    def _empty_metrics(self) -> PerformanceMetrics:
        return PerformanceMetrics(
            n_trades=0, n_wins=0, n_losses=0, hit_rate=0.0,
            pnl_gross_usd=0.0, pnl_net_usd=0.0, avg_pnl_per_trade=0.0,
            ev_expected_usd=0.0, ev_realized_usd=0.0, ev_efficiency=0.0,
            brier_score=0.0, log_loss=0.0, calibration_buckets=[],
            by_side={}, by_edge_bucket={}, by_market={},
            max_drawdown_usd=0.0, sharpe_ratio=None, profit_factor=0.0,
            avg_hold_hours=0.0,
        )
