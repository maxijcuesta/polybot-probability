"""
Model diagnostics: statistical audit of the naive model and signal quality.

This module answers three fundamental questions:
  1. Does p_model add information beyond p_market?
  2. Does edge_net survive realistic costs?
  3. In which market cohorts (if any) does the model generate real edge?

Design: works both on real DB data (once trades accumulate) and on synthetic
market snapshots for offline analysis before the bot has run.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from ..models import (
    MarketFeatures,
    MarketSnapshot,
    Signal,
    Trade,
    TradeStatus,
    utc_now,
)
from ..execution_engine.fill_model import FillModel
from ..config import BotConfig


# ─── OUTPUT TYPES ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class EdgeDistribution:
    """Summary statistics for a sequence of edge values."""
    n: int
    mean: float
    std: float
    p10: float       # 10th percentile
    p25: float
    p50: float       # median
    p75: float
    p90: float       # 90th percentile
    pct_positive: float   # fraction > 0
    pct_above_2pct: float  # fraction > 0.02 (min viable)
    pct_above_4pct: float  # fraction > 0.04 (strong signal)


@dataclass(slots=True)
class CohortStats:
    label: str
    n_signals: int
    n_actionable: int        # edge_net > threshold
    survival_rate: float     # n_actionable / n_signals
    avg_edge_raw: float
    avg_edge_net: float
    avg_half_spread: float
    avg_p_deviation: float   # mean |p_model - p_market|
    # From real trades (if available)
    n_trades: int = 0
    hit_rate: float | None = None
    avg_pnl: float | None = None


@dataclass(slots=True)
class ModelCorrelation:
    """Measures how much p_model deviates from p_market."""
    n: int
    pearson_r: float          # ≈1.0 means model ~ market (redundant)
    mean_abs_deviation: float  # mean |p_model - p_market|
    max_abs_deviation: float
    std_deviation: float
    # Decomposition of model adjustments
    mean_adj_depth: float     # avg contribution from depth imbalance
    mean_adj_volume: float    # avg contribution from volume
    r_squared: float          # variance explained by p_market alone


@dataclass
class DiagnosticsReport:
    """Full diagnostics output for the model audit."""
    generated_at: str = field(default_factory=lambda: utc_now().isoformat())

    # ── Model redundancy analysis ──────────────────────────────────────────────
    correlation: ModelCorrelation | None = None

    # ── Edge distribution ──────────────────────────────────────────────────────
    edge_raw_dist: EdgeDistribution | None = None
    edge_net_dist: EdgeDistribution | None = None
    survival_rate_vs_market: float | None = None   # % signals with edge_net > 0 (using correct cost model)

    # ── Cohort analysis ───────────────────────────────────────────────────────
    by_spread: list[CohortStats] = field(default_factory=list)
    by_liquidity: list[CohortStats] = field(default_factory=list)
    by_time_to_resolution: list[CohortStats] = field(default_factory=list)
    by_depth: list[CohortStats] = field(default_factory=list)

    # ── Verdict ───────────────────────────────────────────────────────────────
    verdict: str = ""           # "redundant" | "weak_promising" | "useless" | "useful_in_cohorts"
    verdict_detail: str = ""    # 2-3 sentence explanation
    best_cohort: str = ""       # best cohort for this model (or "none")


# ─── MAIN ENGINE ──────────────────────────────────────────────────────────────

class DiagnosticsEngine:
    """
    Runs model diagnostics on either real signals from DB or synthetic data.
    """

    MIN_EDGE_NET = 0.02   # minimum net edge to count as "actionable"

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.fill_model = FillModel(config.costs)

    # ── Public interface ───────────────────────────────────────────────────────

    def analyze_signals(
        self,
        signals: list[dict],              # raw rows from DB
        trades: list[Trade] | None = None,
    ) -> DiagnosticsReport:
        """
        Analyze signals stored in DB.

        Args:
            signals: list of dicts from DB (signal_id, p_market, p_model,
                     edge_raw, edge_net, spread_pct, volume_24h, etc.)
            trades: closed trades for outcome analysis (optional)
        """
        report = DiagnosticsReport()

        if not signals:
            report.verdict = "no_data"
            report.verdict_detail = "No signals in DB yet. Run the bot for at least one cycle."
            return report

        report.correlation = self._correlation_from_db(signals)
        report.edge_raw_dist = self._edge_distribution([s["edge_raw"] for s in signals if s.get("edge_raw") is not None])
        report.edge_net_dist = self._edge_distribution([s["edge_net"] for s in signals if s.get("edge_net") is not None])

        # Survival rate using the correct cost model
        edge_nets = [s["edge_net"] for s in signals if s.get("edge_net") is not None]
        n_survive = sum(1 for e in edge_nets if e > self.MIN_EDGE_NET)
        report.survival_rate_vs_market = n_survive / len(edge_nets) if edge_nets else 0.0

        # Cohort analysis
        report.by_spread = self._cohort_by_spread(signals, trades)
        report.by_liquidity = self._cohort_by_liquidity(signals, trades)
        report.by_time_to_resolution = self._cohort_by_time(signals, trades)
        report.by_depth = self._cohort_by_depth(signals, trades)

        report.verdict, report.verdict_detail, report.best_cohort = self._render_verdict(report)
        return report

    def analyze_theoretical(
        self,
        n_simulations: int = 5_000,
        depth_weight: float = 0.03,
        volume_weight: float = 0.01,
    ) -> DiagnosticsReport:
        """
        Theoretical analysis of the naive model's statistical properties.

        No real data needed — uses the model's math to derive properties.
        Creates synthetic market snapshots covering the realistic spread/price space.
        """
        report = DiagnosticsReport()

        # ── Step 1: simulate market snapshots ─────────────────────────────────
        snapshots, features_list = self._generate_synthetic_markets(n_simulations)

        # ── Step 2: compute p_model for each ──────────────────────────────────
        p_markets = [f.mid_price for f in features_list]
        p_models = self._apply_naive_model(features_list, depth_weight, volume_weight)

        # ── Step 3: correlation ────────────────────────────────────────────────
        report.correlation = self._pearson_correlation(p_markets, p_models, depth_weight, volume_weight, features_list)

        # ── Step 4: edge distribution ──────────────────────────────────────────
        edges_raw = [abs(pm - pk) for pm, pk in zip(p_models, p_markets)]
        report.edge_raw_dist = self._edge_distribution(edges_raw)

        # ── Step 5: edge_net using correct cost model ─────────────────────────
        edges_net = []
        for i, (snap, feats) in enumerate(zip(snapshots, features_list)):
            side_is_yes = p_models[i] > p_markets[i]
            from ..models import Side
            side = Side.YES if side_is_yes else Side.NO
            cost = self.fill_model.estimate(snap, side, size_usd=100.0)
            edge_net = edges_raw[i] - cost.total_pct
            edges_net.append(edge_net)

        report.edge_net_dist = self._edge_distribution(edges_net)
        n_survive = sum(1 for e in edges_net if e > self.MIN_EDGE_NET)
        report.survival_rate_vs_market = n_survive / n_simulations

        # ── Step 6: cohort analysis ────────────────────────────────────────────
        report.by_spread = self._theoretical_cohort_spread(
            snapshots, features_list, p_models, p_markets, edges_raw, edges_net
        )
        report.by_liquidity = self._theoretical_cohort_liquidity(
            snapshots, features_list, p_models, p_markets, edges_raw, edges_net
        )
        report.by_time_to_resolution = self._theoretical_cohort_time(
            snapshots, features_list, p_models, p_markets, edges_raw, edges_net
        )
        report.by_depth = self._theoretical_cohort_depth(
            snapshots, features_list, p_models, p_markets, edges_raw, edges_net
        )

        report.verdict, report.verdict_detail, report.best_cohort = self._render_verdict(report)
        return report

    # ── Correlation helpers ────────────────────────────────────────────────────

    def _correlation_from_db(self, signals: list[dict]) -> ModelCorrelation:
        p_mkt = [s["p_market"] for s in signals if s.get("p_market") is not None]
        p_mdl = [s["p_model"] for s in signals if s.get("p_model") is not None]
        n = min(len(p_mkt), len(p_mdl))
        if n < 2:
            return ModelCorrelation(n=n, pearson_r=0.0, mean_abs_deviation=0.0,
                                    max_abs_deviation=0.0, std_deviation=0.0,
                                    mean_adj_depth=0.0, mean_adj_volume=0.0, r_squared=0.0)
        p_mkt, p_mdl = p_mkt[:n], p_mdl[:n]
        devs = [abs(m - k) for m, k in zip(p_mdl, p_mkt)]
        signed_devs = [m - k for m, k in zip(p_mdl, p_mkt)]
        r = self._pearson_r(p_mkt, p_mdl)
        return ModelCorrelation(
            n=n,
            pearson_r=round(r, 6),
            mean_abs_deviation=round(sum(devs) / n, 6),
            max_abs_deviation=round(max(devs), 6),
            std_deviation=round(self._std(signed_devs), 6),
            mean_adj_depth=0.0,    # not available from DB without features
            mean_adj_volume=0.0,
            r_squared=round(r ** 2, 6),
        )

    def _pearson_correlation(
        self,
        p_markets: list[float],
        p_models: list[float],
        depth_weight: float,
        volume_weight: float,
        features_list: list[MarketFeatures],
    ) -> ModelCorrelation:
        n = len(p_markets)
        devs = [abs(m - k) for m, k in zip(p_models, p_markets)]
        signed_devs = [m - k for m, k in zip(p_models, p_markets)]
        r = self._pearson_r(p_markets, p_models)

        adj_depths = [depth_weight * f.depth_imbalance for f in features_list]
        adj_vols = [volume_weight * (min(f.volume_oi_ratio, 2.0) / 2.0 - 0.5) for f in features_list]

        return ModelCorrelation(
            n=n,
            pearson_r=round(r, 6),
            mean_abs_deviation=round(sum(devs) / n, 6),
            max_abs_deviation=round(max(devs), 6),
            std_deviation=round(self._std(signed_devs), 6),
            mean_adj_depth=round(self._mean(adj_depths), 6),
            mean_adj_volume=round(self._mean(adj_vols), 6),
            r_squared=round(r ** 2, 6),
        )

    # ── Edge distribution ──────────────────────────────────────────────────────

    def _edge_distribution(self, values: list[float]) -> EdgeDistribution:
        if not values:
            return EdgeDistribution(n=0, mean=0, std=0, p10=0, p25=0, p50=0, p75=0, p90=0,
                                    pct_positive=0, pct_above_2pct=0, pct_above_4pct=0)
        n = len(values)
        sorted_v = sorted(values)
        mean = sum(values) / n
        std = self._std(values)
        return EdgeDistribution(
            n=n,
            mean=round(mean, 6),
            std=round(std, 6),
            p10=round(sorted_v[int(n * 0.10)], 6),
            p25=round(sorted_v[int(n * 0.25)], 6),
            p50=round(sorted_v[int(n * 0.50)], 6),
            p75=round(sorted_v[int(n * 0.75)], 6),
            p90=round(sorted_v[int(n * 0.90)], 6),
            pct_positive=round(sum(1 for v in values if v > 0) / n, 4),
            pct_above_2pct=round(sum(1 for v in values if v > 0.02) / n, 4),
            pct_above_4pct=round(sum(1 for v in values if v > 0.04) / n, 4),
        )

    # ── Cohort helpers ─────────────────────────────────────────────────────────

    def _theoretical_cohort_spread(
        self,
        snapshots: list[MarketSnapshot],
        features: list[MarketFeatures],
        p_models: list[float],
        p_markets: list[float],
        edges_raw: list[float],
        edges_net: list[float],
    ) -> list[CohortStats]:
        buckets = [
            ("tight  spread<1%",   lambda s: s.spread_pct < 0.01),
            ("normal spread 1–3%", lambda s: 0.01 <= s.spread_pct < 0.03),
            ("wide   spread 3–5%", lambda s: 0.03 <= s.spread_pct < 0.05),
            ("very_wide spread>5%", lambda s: s.spread_pct >= 0.05),
        ]
        return self._build_cohorts(buckets, snapshots, features, p_models, p_markets, edges_raw, edges_net, key="spread")

    def _theoretical_cohort_liquidity(
        self,
        snapshots, features, p_models, p_markets, edges_raw, edges_net,
    ) -> list[CohortStats]:
        buckets = [
            ("low_liq    vol<10k",   lambda s: s.volume_24h < 10_000),
            ("mid_liq  10k–50k",    lambda s: 10_000 <= s.volume_24h < 50_000),
            ("high_liq  50k–200k",  lambda s: 50_000 <= s.volume_24h < 200_000),
            ("very_high  vol>200k", lambda s: s.volume_24h >= 200_000),
        ]
        return self._build_cohorts(buckets, snapshots, features, p_models, p_markets, edges_raw, edges_net, key="volume")

    def _theoretical_cohort_time(
        self,
        snapshots, features, p_models, p_markets, edges_raw, edges_net,
    ) -> list[CohortStats]:
        buckets = [
            ("short  <24h",     lambda f: f.hours_to_resolution < 24),
            ("medium 24–72h",   lambda f: 24 <= f.hours_to_resolution < 72),
            ("long   72h–7d",   lambda f: 72 <= f.hours_to_resolution < 168),
            ("very_long  >7d",  lambda f: f.hours_to_resolution >= 168),
        ]
        return self._build_cohorts(buckets, snapshots, features, p_models, p_markets, edges_raw, edges_net, key="time")

    def _theoretical_cohort_depth(
        self,
        snapshots, features, p_models, p_markets, edges_raw, edges_net,
    ) -> list[CohortStats]:
        buckets = [
            ("thin   depth<500",     lambda f: f.bid_depth_usd < 500),
            ("normal 500–2k",        lambda f: 500 <= f.bid_depth_usd < 2000),
            ("deep   2k–10k",        lambda f: 2000 <= f.bid_depth_usd < 10_000),
            ("very_deep  depth>10k", lambda f: f.bid_depth_usd >= 10_000),
        ]
        return self._build_cohorts(buckets, snapshots, features, p_models, p_markets, edges_raw, edges_net, key="depth")

    def _build_cohorts(
        self,
        buckets: list[tuple],
        snapshots: list[MarketSnapshot],
        features: list[MarketFeatures],
        p_models: list[float],
        p_markets: list[float],
        edges_raw: list[float],
        edges_net: list[float],
        key: str,
    ) -> list[CohortStats]:
        result = []
        for label, cond in buckets:
            if key in ("spread", "volume"):
                indices = [i for i, s in enumerate(snapshots) if cond(s)]
            else:  # depth, time — use features
                indices = [i for i, f in enumerate(features) if cond(f)]

            if not indices:
                continue
            n = len(indices)
            er = [edges_raw[i] for i in indices]
            en = [edges_net[i] for i in indices]
            pm = [p_models[i] for i in indices]
            pk = [p_markets[i] for i in indices]
            devs = [abs(m - k) for m, k in zip(pm, pk)]

            # Compute avg half_spread for this cohort
            half_spreads = [snapshots[i].spread / 2.0 for i in indices]

            n_actionable = sum(1 for e in en if e > self.MIN_EDGE_NET)
            result.append(CohortStats(
                label=label,
                n_signals=n,
                n_actionable=n_actionable,
                survival_rate=round(n_actionable / n, 4),
                avg_edge_raw=round(self._mean(er), 5),
                avg_edge_net=round(self._mean(en), 5),
                avg_half_spread=round(self._mean(half_spreads), 5),
                avg_p_deviation=round(self._mean(devs), 5),
            ))
        return result

    # ── DB cohort analysis ─────────────────────────────────────────────────────

    def _cohort_by_spread(self, signals, trades):
        buckets = [
            ("tight  <1%",   lambda s: (s.get("spread_pct") or 1.0) < 0.01),
            ("normal 1-3%",  lambda s: 0.01 <= (s.get("spread_pct") or 1.0) < 0.03),
            ("wide   3-5%",  lambda s: 0.03 <= (s.get("spread_pct") or 1.0) < 0.05),
            ("wide   >5%",   lambda s: (s.get("spread_pct") or 1.0) >= 0.05),
        ]
        return self._db_cohorts(buckets, signals, trades)

    def _cohort_by_liquidity(self, signals, trades):
        buckets = [
            ("low   <10k",    lambda s: (s.get("volume_24h") or 0) < 10_000),
            ("mid   10–50k",  lambda s: 10_000 <= (s.get("volume_24h") or 0) < 50_000),
            ("high  50–200k", lambda s: 50_000 <= (s.get("volume_24h") or 0) < 200_000),
            ("very  >200k",   lambda s: (s.get("volume_24h") or 0) >= 200_000),
        ]
        return self._db_cohorts(buckets, signals, trades)

    def _cohort_by_time(self, signals, trades):
        buckets = [
            ("short <24h",   lambda s: (s.get("hours_to_resolution") or 999) < 24),
            ("mid  24-72h",  lambda s: 24 <= (s.get("hours_to_resolution") or 999) < 72),
            ("long 72h-7d",  lambda s: 72 <= (s.get("hours_to_resolution") or 999) < 168),
            ("vlong  >7d",   lambda s: (s.get("hours_to_resolution") or 999) >= 168),
        ]
        return self._db_cohorts(buckets, signals, trades)

    def _cohort_by_depth(self, signals, trades):
        buckets = [
            ("thin  <500",   lambda s: (s.get("bid_depth_usd") or 0) < 500),
            ("norm  500-2k", lambda s: 500 <= (s.get("bid_depth_usd") or 0) < 2000),
            ("deep  2-10k",  lambda s: 2000 <= (s.get("bid_depth_usd") or 0) < 10_000),
            ("vdeep >10k",   lambda s: (s.get("bid_depth_usd") or 0) >= 10_000),
        ]
        return self._db_cohorts(buckets, signals, trades)

    def _db_cohorts(self, buckets, signals, trades):
        trades_by_market = defaultdict(list)
        if trades:
            for t in trades:
                if t.status == TradeStatus.CLOSED:
                    trades_by_market[t.market_id].append(t)
        result = []
        for label, cond in buckets:
            group = [s for s in signals if cond(s)]
            if not group:
                continue
            er = [s["edge_raw"] for s in group if s.get("edge_raw") is not None]
            en = [s["edge_net"] for s in group if s.get("edge_net") is not None]
            half_spreads = [(s.get("spread_pct") or 0) / 2.0 for s in group]
            n_actionable = sum(1 for e in en if e > self.MIN_EDGE_NET)
            devs = [abs((s.get("p_model") or 0) - (s.get("p_market") or 0)) for s in group]

            # Trade metrics for this cohort
            mkt_ids = {s["market_id"] for s in group}
            cohort_trades = [t for mid in mkt_ids for t in trades_by_market.get(mid, [])]
            hit_rate = None
            avg_pnl = None
            if cohort_trades:
                wins = sum(1 for t in cohort_trades if (t.pnl_usd or 0) > 0)
                hit_rate = round(wins / len(cohort_trades), 4)
                avg_pnl = round(sum(t.pnl_usd or 0 for t in cohort_trades) / len(cohort_trades), 4)

            result.append(CohortStats(
                label=label,
                n_signals=len(group),
                n_actionable=n_actionable,
                survival_rate=round(n_actionable / len(group), 4) if group else 0.0,
                avg_edge_raw=round(self._mean(er), 5),
                avg_edge_net=round(self._mean(en), 5),
                avg_half_spread=round(self._mean(half_spreads), 5),
                avg_p_deviation=round(self._mean(devs), 5),
                n_trades=len(cohort_trades),
                hit_rate=hit_rate,
                avg_pnl=avg_pnl,
            ))
        return result

    # ── Verdict ────────────────────────────────────────────────────────────────

    def _render_verdict(self, report: DiagnosticsReport) -> tuple[str, str, str]:
        """
        Classify the model into one of four states and identify the best cohort.
        """
        if report.correlation is None:
            return "no_data", "Run the bot to collect signal data.", "none"

        r = report.correlation.pearson_r
        mad = report.correlation.mean_abs_deviation
        survival = report.survival_rate_vs_market or 0.0
        edge_net_p90 = report.edge_net_dist.p90 if report.edge_net_dist else 0.0

        # Find best cohort by survival rate
        all_cohorts = (
            report.by_spread + report.by_liquidity +
            report.by_time_to_resolution + report.by_depth
        )
        best = max(all_cohorts, key=lambda c: c.survival_rate) if all_cohorts else None
        best_label = f"{best.label} (survival={best.survival_rate:.1%})" if best else "none"

        # Decision tree
        if r > 0.9999 and mad < 0.005:
            verdict = "redundant"
            detail = (
                f"p_model correlates with p_market at r={r:.5f} with mean deviation {mad*100:.2f}%. "
                f"The model adds essentially no new information beyond the market price. "
                f"Survival rate after costs: {survival:.1%}. Edge is almost entirely noise."
            )
        elif survival < 0.05:
            verdict = "useless"
            detail = (
                f"Only {survival:.1%} of signals survive realistic transaction costs. "
                f"The model's maximum adjustment ({report.correlation.max_abs_deviation*100:.1f}%) "
                f"is too small to clear the half-spread. Not tradeable in current form."
            )
        elif survival < 0.15 and (best is None or best.survival_rate < 0.20):
            verdict = "weak_promising"
            detail = (
                f"Survival rate is low ({survival:.1%}) but the model shows signal in "
                f"specific cohorts. Mean |p_model - p_market| = {mad*100:.2f}%. "
                f"Best cohort: {best_label}. Needs more data and a trained model to improve."
            )
        else:
            verdict = "useful_in_cohorts"
            detail = (
                f"The model generates actionable edge ({survival:.1%} survival rate) "
                f"particularly in certain cohorts. Best cohort: {best_label}. "
                f"Focus on these markets and consider training a dedicated model here."
            )

        return verdict, detail, best_label

    # ── Synthetic market generation ────────────────────────────────────────────

    def _generate_synthetic_markets(
        self, n: int
    ) -> tuple[list[MarketSnapshot], list[MarketFeatures]]:
        """
        Generate synthetic market snapshots covering realistic Polymarket conditions.
        Distributions are based on empirical Polymarket market characteristics.
        """
        import random
        rng = random.Random(42)  # deterministic seed

        from ..models import MarketSnapshot, MarketFeatures, Orderbook, OrderbookLevel
        from datetime import datetime, timezone, timedelta

        snapshots = []
        features = []
        now = utc_now()

        for _ in range(n):
            # Mid price: most markets cluster near 0.1-0.9
            mid = rng.uniform(0.05, 0.95)

            # Spread: log-normal, most markets 1-4%, some tighter/wider
            spread_pct = max(0.002, min(0.15, rng.lognormvariate(-4.0, 0.8)))
            spread = spread_pct * mid * 2  # spread in price units

            best_bid = max(0.01, mid - spread / 2)
            best_ask = min(0.99, mid + spread / 2)

            # Volume: log-normal, $5k–$500k typical
            volume_24h = rng.lognormvariate(10.5, 1.2)  # median ~$36k
            open_interest = volume_24h * rng.uniform(0.5, 5.0)

            # Depth: correlated with OI
            bid_depth = open_interest * rng.uniform(0.05, 0.25)
            ask_depth = open_interest * rng.uniform(0.05, 0.25)

            # Depth imbalance: uniform -0.5 to 0.5 (mild bias)
            depth_imbalance = rng.uniform(-0.5, 0.5)
            # Adjust depths to match imbalance
            total_depth = bid_depth + ask_depth
            bid_depth = total_depth * (0.5 + depth_imbalance / 2)
            ask_depth = total_depth * (0.5 - depth_imbalance / 2)

            # Time to resolution: wide range
            hours_to_res = rng.lognormvariate(3.5, 1.5)  # median ~33 hours

            # Last trade: recent for active markets
            hours_since_trade = rng.lognormvariate(0.5, 1.0)

            vol_oi_ratio = volume_24h / max(open_interest, 1.0)

            ob = Orderbook(
                bids=[OrderbookLevel(price=best_bid, size=bid_depth / best_bid)],
                asks=[OrderbookLevel(price=best_ask, size=ask_depth / best_ask)],
            )

            snap = MarketSnapshot(
                market_id=f"syn-{_:04d}",
                condition_id=f"cond-{_:04d}",
                question="Synthetic market?",
                category="SYNTHETIC",
                yes_token_id=f"yes-{_}",
                no_token_id=f"no-{_}",
                best_bid=round(best_bid, 4),
                best_ask=round(best_ask, 4),
                volume_24h=round(volume_24h, 2),
                volume_total=round(volume_24h * 30, 2),
                open_interest=round(open_interest, 2),
                last_trade_price=round(mid, 4),
                last_trade_time=now - timedelta(hours=hours_since_trade),
                resolution_time=now + timedelta(hours=hours_to_res),
                active=True,
                orderbook=ob,
            )
            feat = MarketFeatures(
                market_id=snap.market_id,
                mid_price=round(mid, 4),
                spread_pct=round(spread_pct, 6),
                bid_depth_usd=round(bid_depth, 2),
                ask_depth_usd=round(ask_depth, 2),
                depth_imbalance=round(depth_imbalance, 4),
                volume_24h=round(volume_24h, 2),
                open_interest=round(open_interest, 2),
                volume_oi_ratio=round(vol_oi_ratio, 4),
                hours_to_resolution=round(hours_to_res, 2),
                hours_since_last_trade=round(hours_since_trade, 2),
                is_binary=True,
            )
            snapshots.append(snap)
            features.append(feat)

        return snapshots, features

    def _apply_naive_model(
        self,
        features: list[MarketFeatures],
        depth_weight: float,
        volume_weight: float,
    ) -> list[float]:
        result = []
        for f in features:
            p_base = f.mid_price
            adj_depth = depth_weight * f.depth_imbalance
            vol_factor = min(f.volume_oi_ratio, 2.0) / 2.0
            adj_volume = volume_weight * (vol_factor - 0.5)
            p_yes = max(0.01, min(0.99, p_base + adj_depth + adj_volume))
            result.append(p_yes)
        return result

    # ── Math utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _pearson_r(x: list[float], y: list[float]) -> float:
        n = len(x)
        if n < 2:
            return 0.0
        mx, my = sum(x) / n, sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        dx = (sum((xi - mx) ** 2 for xi in x)) ** 0.5
        dy = (sum((yi - my) ** 2 for yi in y)) ** 0.5
        return num / (dx * dy) if dx * dy > 1e-10 else 1.0

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _std(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        m = sum(values) / len(values)
        return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5
