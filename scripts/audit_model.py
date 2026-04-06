#!/usr/bin/env python3
"""
audit_model.py — Standalone model audit for probabilisticobot.

Usage:
    python scripts/audit_model.py                          # theoretical only
    python scripts/audit_model.py --db data/polybot.db     # real signals from DB
    python scripts/audit_model.py --simulations 10000       # more Monte Carlo samples
    python scripts/audit_model.py --no-theoretical          # skip simulation, DB-only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polybot.analytics.diagnostics import DiagnosticsEngine, DiagnosticsReport


# ─── FORMATTING HELPERS ───────────────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"

def _f4(v: float) -> str:
    return f"{v:.4f}"

def _bar(v: float, width: int = 20) -> str:
    filled = int(v * width)
    return "█" * filled + "░" * (width - filled)


def render_report(report: DiagnosticsReport, title: str = "MODEL AUDIT") -> None:
    """Print a formatted audit report to stdout."""

    BOLD = "\033[1m"
    RESET = "\033[0m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"

    def color_verdict(v: str) -> str:
        colors = {
            "redundant": RED,
            "useless": RED,
            "weak_promising": YELLOW,
            "useful_in_cohorts": GREEN,
        }
        return colors.get(v, CYAN) + v.upper() + RESET

    print()
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{'=' * 60}")

    # ── Correlation ──────────────────────────────────────────────────────────
    corr = report.model_correlation
    print(f"\n{BOLD}[1] MODEL vs MARKET CORRELATION{RESET}")
    print(f"  Pearson r         : {_f4(corr.pearson_r)}")
    print(f"  Mean Abs Deviation: {_pct(corr.mean_abs_deviation)}")
    print(f"  Max abs deviation : {_pct(corr.max_abs_deviation)}")
    print(f"  Interpretation    : {corr.interpretation}")

    # ── Edge distribution ────────────────────────────────────────────────────
    edg = report.edge_distribution
    print(f"\n{BOLD}[2] EDGE DISTRIBUTION (raw){RESET}")
    print(f"  Mean              : {_pct(edg.mean_edge_raw)}")
    print(f"  Median            : {_pct(edg.median_edge_raw)}")
    print(f"  Std dev           : {_pct(edg.std_edge_raw)}")
    print(f"  p10 / p25 / p75 / p90:")
    p = edg.percentiles
    print(f"    {_pct(p.get('p10', 0))} / {_pct(p.get('p25', 0))} / {_pct(p.get('p75', 0))} / {_pct(p.get('p90', 0))}")

    edgn = report.edge_net_distribution
    if edgn:
        print(f"\n{BOLD}[3] EDGE DISTRIBUTION (net, after costs){RESET}")
        print(f"  Mean              : {_pct(edgn.mean_edge_raw)}")
        print(f"  Median            : {_pct(edgn.median_edge_raw)}")
        print(f"  Survival rate     : {_pct(edgn.survival_rate)}  {_bar(edgn.survival_rate)}")
        print(f"  n signals total   : {edgn.n_signals}")
        print(f"  n actionable      : {edgn.n_actionable}")

    # ── Cohort analysis ──────────────────────────────────────────────────────
    def render_cohorts(label: str, cohorts: dict) -> None:
        if not cohorts:
            return
        print(f"\n{BOLD}[COHORT] {label}{RESET}")
        print(f"  {'Cohort':<18} {'Survival':>9} {'Mean edge_net':>14} {'n':>6}")
        print(f"  {'-' * 52}")
        for name, stats in cohorts.items():
            sr = stats.survival_rate
            color = GREEN if sr > 0.15 else (YELLOW if sr > 0.05 else RED)
            print(
                f"  {name:<18} "
                f"{color}{_pct(sr):>9}{RESET} "
                f"{_pct(stats.mean_edge_net):>14} "
                f"{stats.n_signals:>6}"
            )

    render_cohorts("SPREAD (low / mid / high)", report.cohorts_by_spread)
    render_cohorts("LIQUIDITY (vol/OI ratio)", report.cohorts_by_liquidity)
    render_cohorts("TIME TO RESOLUTION", report.cohorts_by_time_to_resolution)
    render_cohorts("DEPTH (ask side)", report.cohorts_by_depth)

    # ── Final verdict ────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"{BOLD}  VERDICT: {color_verdict(report.verdict)}{RESET}")
    print(f"  {report.verdict_explanation}")
    print(f"{'=' * 60}\n")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def _load_signals_from_db(db_path: str) -> list[dict]:
    """Pull signals from DB with cohort fields."""
    try:
        import aiosqlite
    except ImportError:
        print("aiosqlite not installed — skipping DB analysis.")
        return []

    signals = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT signal_id, market_id, side, p_market, p_model, p_calibrated,
                   edge_raw, edge_net, guard_passed,
                   spread_pct, volume_24h, open_interest, hours_to_resolution, bid_depth_usd
            FROM signals
            ORDER BY created_at DESC
            LIMIT 5000
            """
        ) as cursor:
            rows = await cursor.fetchall()
    for row in rows:
        signals.append(dict(row))
    return signals


async def main() -> None:
    parser = argparse.ArgumentParser(description="Model audit for probabilisticobot")
    parser.add_argument("--db", default="", help="Path to polybot.db (optional)")
    parser.add_argument("--simulations", type=int, default=5000, help="Synthetic simulations (default 5000)")
    parser.add_argument("--no-theoretical", action="store_true", help="Skip Monte Carlo simulation")
    parser.add_argument("--depth-weight", type=float, default=0.03)
    parser.add_argument("--volume-weight", type=float, default=0.01)
    args = parser.parse_args()

    engine = DiagnosticsEngine()

    # ── Theoretical / Monte Carlo analysis ──────────────────────────────────
    if not args.no_theoretical:
        print(f"\nRunning Monte Carlo simulation ({args.simulations} synthetic markets)...")
        report = engine.analyze_theoretical(
            n_simulations=args.simulations,
            depth_weight=args.depth_weight,
            volume_weight=args.volume_weight,
        )
        render_report(report, title="THEORETICAL AUDIT (synthetic markets)")

    # ── Real data analysis (if DB provided and exists) ───────────────────────
    if args.db and Path(args.db).exists():
        print(f"\nLoading signals from {args.db}...")
        signals = await _load_signals_from_db(args.db)
        if signals:
            print(f"Loaded {len(signals)} signals. Analyzing...")
            report = engine.analyze_signals(signals)
            render_report(report, title=f"REAL DATA AUDIT ({len(signals)} signals from DB)")
        else:
            print("No signals found in DB yet.")
    elif args.db:
        print(f"DB not found at {args.db} — skipping real data analysis.")


if __name__ == "__main__":
    asyncio.run(main())
