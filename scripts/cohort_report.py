#!/usr/bin/env python3
"""
Cohort report — does final_trade_score correlate with better outcomes?

Answers the core validation question:
    "Does InefficiencyScorer select trades that are actually better
     than a trivial baseline (random selection)?"

Methodology
-----------
1. Load all closed trades that have a final_trade_score stored.
2. Sort by final_trade_score, divide into quintiles (Q1=lowest, Q5=highest).
3. Per quintile compute: hit_rate, avg_pnl, sum_pnl, Brier score (if outcomes
   available), direction accuracy (suggested_side vs actual resolution).
4. Compare Q5 vs Q1 and vs overall baseline.
5. Emit a clear VEREDICTO with one of:
   - "scorer CORRELACIONA con outcomes" — Q5 significantly outperforms Q1
   - "sin correlación detectable" — differences are within noise
   - "datos insuficientes" — fewer than MIN_TRADES_FOR_VERDICT closed trades

Also shows the full scored-signal universe (not just executed trades) so you
can see if the score distribution of executed vs non-executed markets differs.

Usage
-----
    python scripts/cohort_report.py
    python scripts/cohort_report.py --db ./data/polybot.db
    python scripts/cohort_report.py --db ./data/polybot.db --min-trades 10

Exit codes
----------
    0  Report generated (regardless of verdict)
    1  Fatal error (DB not found, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.polybot import db as storage  # noqa: E402


# ── Constants ──────────────────────────────────────────────────────────────────
MIN_TRADES_FOR_VERDICT = 10   # minimum closed trades to draw any conclusion
Q5_VS_Q1_THRESHOLD = 0.10     # Q5 hit rate must exceed Q1 by this to claim signal
N_QUINTILES = 5


# ── Statistics ─────────────────────────────────────────────────────────────────

def _hit_rate(rows: list[dict]) -> float | None:
    """Fraction of trades where pnl_usd > 0. None if no rows."""
    if not rows:
        return None
    wins = sum(1 for r in rows if (r.get("pnl_usd") or 0.0) > 0)
    return wins / len(rows)


def _avg_pnl(rows: list[dict]) -> float | None:
    pnls = [r["pnl_usd"] for r in rows if r.get("pnl_usd") is not None]
    return sum(pnls) / len(pnls) if pnls else None


def _sum_pnl(rows: list[dict]) -> float:
    return sum(r["pnl_usd"] for r in rows if r.get("pnl_usd") is not None)


def _brier_score(rows: list[dict]) -> float | None:
    """
    Brier score = mean((p_calibrated - outcome)^2).
    Lower is better (0 = perfect). None if no resolved trades.
    """
    resolved = [
        r for r in rows
        if r.get("outcome") is not None and r.get("p_calibrated") is not None
    ]
    if not resolved:
        return None
    return sum(
        (r["p_calibrated"] - r["outcome"]) ** 2 for r in resolved
    ) / len(resolved)


def _direction_accuracy(rows: list[dict]) -> tuple[float | None, int]:
    """
    Fraction of rows where suggested_side matches the winning outcome.

    suggested_side="YES" is correct when outcome=1 (YES won).
    suggested_side="NO"  is correct when outcome=0 (NO won).

    Returns (accuracy, n_resolved).
    """
    relevant = []
    for r in rows:
        outcome = r.get("outcome")
        suggested = r.get("suggested_side")
        if outcome is None or not suggested:
            continue
        correct = "YES" if outcome == 1 else "NO"
        relevant.append(1 if suggested == correct else 0)
    if not relevant:
        return None, 0
    return sum(relevant) / len(relevant), len(relevant)


def _quintile_groups(rows: list[dict]) -> list[list[dict]]:
    """
    Split rows into N_QUINTILES equal groups by final_trade_score ascending.
    Q1 = lowest-scored, Q5 = highest-scored.
    """
    sorted_rows = sorted(rows, key=lambda r: r.get("final_trade_score") or 0.0)
    n = len(sorted_rows)
    if n < N_QUINTILES:
        # Too few: one group per row, no quintile split
        return [[r] for r in sorted_rows] if n else []
    size = n // N_QUINTILES
    groups = []
    for i in range(N_QUINTILES):
        start = i * size
        end = start + size if i < N_QUINTILES - 1 else n
        groups.append(sorted_rows[start:end])
    return groups


# ── Rendering ──────────────────────────────────────────────────────────────────

def _fmt(val: float | None, fmt: str = ".2f", suffix: str = "") -> str:
    if val is None:
        return "—"
    return f"{val:{fmt}}{suffix}"


def _render_cohort_table(groups: list[list[dict]], all_rows: list[dict]) -> None:
    W = 96
    print(f"\n{'─'*W}")
    print(f"  COHORT REPORT  —  InefficiencyScorer vs Outcomes")
    print(f"  closed trades with score data: {len(all_rows)}")
    print(f"{'─'*W}")

    if not all_rows:
        print("\n  No hay trades cerrados con score todavía. Deja correr el bot más tiempo.\n")
        return

    hdr = (
        f"{'Quintil':<12}"
        f"{'N':>5}"
        f"{'ScoreRange':>18}"
        f"{'HitRate':>9}"
        f"{'AvgPnL':>9}"
        f"{'SumPnL':>10}"
        f"{'Brier':>8}"
        f"{'DirAcc':>8}"
        f"{'nDir':>6}"
    )
    print(f"\n{hdr}")
    print(f"{'─'*W}")

    for i, group in enumerate(groups, 1):
        if not group:
            continue
        scores = [r["final_trade_score"] for r in group if r.get("final_trade_score") is not None]
        score_range = (
            f"{min(scores):.3f}–{max(scores):.3f}" if len(scores) > 1
            else f"{scores[0]:.3f}" if scores else "N/A"
        )
        n = len(group)
        hr = _hit_rate(group)
        avg = _avg_pnl(group)
        total = _sum_pnl(group)
        bs = _brier_score(group)
        da, n_dir = _direction_accuracy(group)

        label = (
            f"Q{i} (LOW)"  if i == 1
            else f"Q{i} (HIGH)" if i == len(groups)
            else f"Q{i}"
        )
        print(
            f"{label:<12}"
            f"{n:>5}"
            f"{score_range:>18}"
            f"{_fmt(hr, '.1%'):>9}"
            f"{_fmt(avg, '.2f', '$'):>9}"
            f"{_fmt(total, '.2f', '$'):>10}"
            f"{_fmt(bs, '.4f'):>8}"
            f"{_fmt(da, '.1%'):>8}"
            f"{n_dir:>6}"
        )

    # Overall baseline row
    print(f"{'─'*W}")
    hr_all = _hit_rate(all_rows)
    avg_all = _avg_pnl(all_rows)
    total_all = _sum_pnl(all_rows)
    bs_all = _brier_score(all_rows)
    da_all, n_dir_all = _direction_accuracy(all_rows)
    print(
        f"{'TOTAL/BASE':<12}"
        f"{len(all_rows):>5}"
        f"{'':>18}"
        f"{_fmt(hr_all, '.1%'):>9}"
        f"{_fmt(avg_all, '.2f', '$'):>9}"
        f"{_fmt(total_all, '.2f', '$'):>10}"
        f"{_fmt(bs_all, '.4f'):>8}"
        f"{_fmt(da_all, '.1%'):>8}"
        f"{n_dir_all:>6}"
    )

    print(f"\n  Columns: N=trades  HitRate=pnl>0/n  AvgPnL=mean pnl  SumPnL=total")
    print(f"  Brier=calibration error (lower=better, requires resolved outcomes)")
    print(f"  DirAcc=suggested_side vs correct resolution (requires resolved outcomes)")


def _render_signal_universe(rows: list[dict]) -> None:
    """Show breakdown of scored signals: executed vs non-executed."""
    if not rows:
        return
    executed = [r for r in rows if r.get("acted_on")]
    non_exec = [r for r in rows if not r.get("acted_on")]
    scores_exec = [r["final_trade_score"] for r in executed if r.get("final_trade_score")]
    scores_non  = [r["final_trade_score"] for r in non_exec  if r.get("final_trade_score")]

    print(f"\n  UNIVERSO DE SEÑALES CON SCORE")
    print(f"  {'Total scored (guard-passed):':<36} {len(rows)}")
    print(f"  {'Executed (acted_on=1):':<36} {len(executed)}"
          + (f"  avg_score={sum(scores_exec)/len(scores_exec):.3f}" if scores_exec else ""))
    print(f"  {'Not executed (below threshold):':<36} {len(non_exec)}"
          + (f"  avg_score={sum(scores_non)/len(scores_non):.3f}" if scores_non else ""))
    if scores_exec and scores_non:
        print(
            f"\n  Diferencia de score ejecutados vs no-ejecutados: "
            f"{(sum(scores_exec)/len(scores_exec)) - (sum(scores_non)/len(scores_non)):+.3f}"
        )
        print(f"  (positivo = el filtro seleccionó mercados con mayor score, como se espera)")


def _render_verdict(groups: list[list[dict]], all_rows: list[dict]) -> None:
    W = 96
    print(f"\n{'═'*W}")
    print(f"  VEREDICTO")
    print(f"{'═'*W}")

    n_closed = len(all_rows)

    if n_closed < MIN_TRADES_FOR_VERDICT:
        print(f"\n  DATOS INSUFICIENTES")
        print(f"  Solo {n_closed} trade(s) cerrado(s) con score. Se necesitan ≥ {MIN_TRADES_FOR_VERDICT}.")
        print(f"  Sin conclusión posible. Deja correr el bot y vuelve a ejecutar este reporte.")
        print(f"\n{'═'*W}\n")
        return

    if len(groups) < 2:
        print(f"\n  DATOS INSUFICIENTES PARA QUINTILES ({n_closed} trades)")
        print(f"  Se necesitan ≥ {N_QUINTILES} trades para separar en quintiles.")
        print(f"\n{'═'*W}\n")
        return

    hr_q5 = _hit_rate(groups[-1])
    hr_q1 = _hit_rate(groups[0])
    avg_q5 = _avg_pnl(groups[-1])
    avg_q1 = _avg_pnl(groups[0])

    pnl_signal = (
        avg_q5 is not None and avg_q1 is not None and avg_q5 > avg_q1
    )
    hr_signal = (
        hr_q5 is not None and hr_q1 is not None
        and hr_q5 > hr_q1 + Q5_VS_Q1_THRESHOLD
    )

    # Direction accuracy across all resolved trades
    da_all, n_dir = _direction_accuracy(all_rows)
    dir_signal = da_all is not None and n_dir >= 5 and da_all > 0.55

    if hr_signal and pnl_signal:
        print(f"\n  scorer CORRELACIONA con outcomes")
        print(f"  Q5 hit rate ({_fmt(hr_q5, '.1%')}) supera Q1 ({_fmt(hr_q1, '.1%')}) "
              f"en más de {Q5_VS_Q1_THRESHOLD:.0%}.")
        print(f"  Q5 avg PnL ({_fmt(avg_q5, '.2f', '$')}) > Q1 ({_fmt(avg_q1, '.2f', '$')}).")
        print(f"  Evidencia inicial de señal. Ampliar muestra antes de ajustar thresholds.")
        if dir_signal:
            print(f"  Direction accuracy ({_fmt(da_all, '.1%')}) > 55% — scorer tiene valor direccional.")
    else:
        print(f"\n  SIN CORRELACIÓN DETECTABLE entre final_trade_score y outcomes.")
        if hr_q5 is not None and hr_q1 is not None:
            diff = hr_q5 - hr_q1
            print(f"  Q5 hit rate ({_fmt(hr_q5, '.1%')}) vs Q1 ({_fmt(hr_q1, '.1%')}) "
                  f"— diferencia: {diff:+.1%} (umbral mínimo: +{Q5_VS_Q1_THRESHOLD:.0%}).")
        if not pnl_signal and avg_q5 is not None and avg_q1 is not None:
            print(f"  Q5 avg PnL ({_fmt(avg_q5, '.2f', '$')}) no supera Q1 ({_fmt(avg_q1, '.2f', '$')}).")
        print()
        print(f"  CONCLUSIÓN: el scorer actual NO demuestra edge sobre una baseline trivial.")
        print(f"  Ver STATUS.md — sección 'CLOB y evidencia de edge'.")
        print()
        print(f"  Causas probables:")
        print(f"    1. Sin CLOB: depth_imbalance=0 → microprice_gap=0 y book_imbalance=0 siempre.")
        print(f"       El scorer rankea por calidad de ejecución, no por mispricing real.")
        print(f"    2. Muestra aún pequeña ({n_closed} trades): necesitas ≥30 para reducir ruido.")
        print(f"    3. Fixed-fraction sizing (sin Kelly real) — todos los trades tienen mismo tamaño.")

    if da_all is not None and n_dir > 0:
        print(f"\n  Direction accuracy: {_fmt(da_all, '.1%')} sobre {n_dir} trades resueltos "
              f"({'mejor que random' if da_all > 0.50 else 'no mejor que random'}).")
    else:
        print(f"\n  Direction accuracy: no calculable aún (0 trades resueltos con outcome conocido).")
        print(f"  El mercado debe resolver (YES o NO) para medir calibración real.")

    print(f"\n{'═'*W}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(db_path: str) -> None:
    try:
        trades = await storage.load_cohort_data(db_path)
        signals = await storage.load_all_scored_signals(db_path)
    except Exception as e:
        print(f"\nError cargando datos de {db_path}: {e}")
        print("¿El bot corrió al menos un ciclo? Verifica que el archivo DB exista.")
        sys.exit(1)

    print(f"\n  DB: {db_path}")
    _render_signal_universe(signals)

    groups = _quintile_groups(trades)
    _render_cohort_table(groups, trades)
    _render_verdict(groups, trades)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cohort report: does final_trade_score correlate with outcomes?",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default="./data/polybot.db",
        help="Path to SQLite DB (default: ./data/polybot.db)",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"\nDB no encontrada: {args.db}")
        print("Opciones: --db /ruta/al/archivo.db  o  fly ssh console → sqlite3 /data/polybot.db")
        sys.exit(1)

    asyncio.run(run(args.db))


if __name__ == "__main__":
    main()
