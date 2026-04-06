#!/usr/bin/env python3
"""
funnel_audit.py — Auditoría de funnel completa del bot de Polymarket.

Lee la DB SQLite real y responde:
  A. ¿Dónde mueren las señales? (pirámide de funnel)
  B. ¿Por qué las rechaza / recorta el risk engine?
  C. ¿Qué cohortes tienen edge real después de costos?
  D. Veredicto explícito: ¿hay señal o no?

Uso:
    python scripts/funnel_audit.py --db data/polybot.db
    python scripts/funnel_audit.py --db data/polybot.db --min-n 5
    python scripts/funnel_audit.py --db data/polybot.db --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    import aiosqlite
except ImportError:
    print("ERROR: aiosqlite no instalado. Ejecuta: pip install aiosqlite")
    sys.exit(1)


# ─── COLORES ANSI ─────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
RESET = "\033[0m"
RED   = "\033[91m"
YEL   = "\033[93m"
GRN   = "\033[92m"
DIM   = "\033[2m"
CYN   = "\033[96m"


def _pct(v: float | None, d: int = 1) -> str:
    if v is None:
        return "   n/a"
    return f"{v * 100:.{d}f}%"


def _usd(v: float | None) -> str:
    if v is None:
        return "  n/a"
    sign = "+" if v > 0 else ""
    return f"{sign}${v:.2f}"


def _color_rate(rate: float | None, lo: float = 0.05, hi: float = 0.15) -> str:
    if rate is None:
        return DIM + "   n/a" + RESET
    s = _pct(rate)
    if rate < lo:
        return RED + s + RESET
    if rate < hi:
        return YEL + s + RESET
    return GRN + s + RESET


def _color_num(v: float | None) -> str:
    if v is None:
        return DIM + "   n/a" + RESET
    if v > 0:
        return GRN + f"+{v:.3f}" + RESET
    if v < 0:
        return RED + f"{v:.3f}" + RESET
    return f"{v:.3f}"


def _bar(rate: float, width: int = 18) -> str:
    filled = min(int(max(rate, 0.0) * width), width)
    return "█" * filled + "░" * (width - filled)


# ─── CARGA DESDE DB ───────────────────────────────────────────────────────────

async def _load_funnel_totals(db: aiosqlite.Connection) -> dict[str, int]:
    """Suma acumulada de cada event_name en funnel_events (EAV)."""
    async with db.execute(
        "SELECT event_name, SUM(event_count) FROM funnel_events GROUP BY event_name"
    ) as cur:
        rows = await cur.fetchall()
    return {r[0]: (r[1] or 0) for r in rows}


async def _load_n_cycles(db: aiosqlite.Connection) -> int:
    """Número exacto de ciclos distintos registrados en funnel_events."""
    async with db.execute("SELECT COUNT(DISTINCT cycle_id) FROM funnel_events") as cur:
        row = await cur.fetchone()
    return (row[0] or 0) if row else 0


async def _load_capital_stats(db: aiosqlite.Connection) -> dict:
    """
    Agrega datos de capital (USD) desde la tabla signals.

    Campos usados: requested_size_usd, approved_size_usd, trim_reason.
    Solo considera señales con requested_size_usd NOT NULL (es decir, las que
    llegaron al risk engine).
    """
    async with db.execute(
        """
        SELECT
            COUNT(*)                                                     AS n_sized,
            COALESCE(SUM(requested_size_usd), 0)                        AS total_requested,
            COALESCE(SUM(approved_size_usd),  0)                        AS total_approved,
            COALESCE(SUM(
                CASE WHEN trim_reason IS NOT NULL
                     THEN requested_size_usd - approved_size_usd
                     ELSE 0 END
            ), 0)                                                        AS total_trimmed
        FROM signals
        WHERE requested_size_usd IS NOT NULL
        """
    ) as cur:
        row = await cur.fetchone()

    n_sized, total_req, total_app, total_trim = row if row else (0, 0.0, 0.0, 0.0)

    # Capital recortado por trim_reason
    async with db.execute(
        """
        SELECT
            trim_reason,
            COUNT(*)                                          AS n,
            COALESCE(SUM(requested_size_usd - approved_size_usd), 0) AS trimmed_usd
        FROM signals
        WHERE trim_reason IS NOT NULL
          AND requested_size_usd IS NOT NULL
        GROUP BY trim_reason
        ORDER BY trimmed_usd DESC
        """
    ) as cur:
        trim_by_reason = await cur.fetchall()

    return {
        "n_sized":       int(n_sized or 0),
        "total_requested": float(total_req or 0),
        "total_approved":  float(total_app or 0),
        "total_trimmed":   float(total_trim or 0),
        "trim_by_reason":  [(r[0], int(r[1] or 0), float(r[2] or 0)) for r in trim_by_reason],
    }


async def _load_signals(db: aiosqlite.Connection, limit: int) -> list[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT
            s.signal_id, s.market_id, s.side,
            s.p_market, s.p_model, s.p_calibrated,
            s.edge_raw, s.edge_net,
            s.guard_passed, s.guard_failures,
            s.acted_on,
            s.spread_pct, s.volume_24h, s.open_interest,
            s.hours_to_resolution, s.bid_depth_usd,
            s.fill_total_pct,
            s.requested_size_usd, s.approved_size_usd,
            s.reject_reason, s.trim_reason, s.risk_limited,
            t.trade_id, t.status AS trade_status,
            t.pnl_usd, t.outcome, t.entry_price, t.exit_price
        FROM signals s
        LEFT JOIN trades t ON t.signal_id = s.signal_id
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    db.row_factory = None
    return [dict(r) for r in rows]


async def _load_reject_breakdown(db: aiosqlite.Connection) -> list[tuple[str, int]]:
    async with db.execute(
        """
        SELECT COALESCE(reject_reason, '(sin_datos)') AS r, COUNT(*) AS n
        FROM signals
        WHERE guard_passed = 1
          AND (edge_net IS NOT NULL AND edge_net > 0)
          AND (approved_size_usd IS NULL OR approved_size_usd = 0)
          AND reject_reason IS NOT NULL
        GROUP BY r
        ORDER BY n DESC
        """
    ) as cur:
        return await cur.fetchall()


async def _load_trim_breakdown(db: aiosqlite.Connection) -> list[tuple[str, int]]:
    async with db.execute(
        """
        SELECT COALESCE(trim_reason, '(sin_datos)') AS r, COUNT(*) AS n
        FROM signals
        WHERE approved_size_usd > 0
          AND risk_limited = 1
          AND trim_reason IS NOT NULL
        GROUP BY r
        ORDER BY n DESC
        """
    ) as cur:
        return await cur.fetchall()


async def _load_guard_failures(db: aiosqlite.Connection) -> list[tuple[str, int]]:
    async with db.execute(
        "SELECT guard_failures FROM signals WHERE guard_passed = 0 AND guard_failures IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    counts: dict[str, int] = defaultdict(int)
    for (raw,) in rows:
        try:
            failures = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            failures = []
        for f in failures:
            counts[f] += 1
        if not failures:
            counts["(desconocido)"] += 1
    return sorted(counts.items(), key=lambda x: -x[1])


# ─── SECCIÓN A: PIRÁMIDE DE FUNNEL ───────────────────────────────────────────

# Orden y etiquetas de la pirámide
_PYRAMID: list[tuple[str, str, str | None]] = [
    # (event_name, etiqueta, event_name_anterior_para_%prev)
    # Invariante: risk_approved + risk_rejected + already_positioned = positive_edge_net
    ("markets_fetched",    "Markets fetched",       None),
    ("passed_guards",      "Passed guards",          "markets_fetched"),
    ("signals_computed",   "Signals computed",       "markets_fetched"),
    ("positive_edge_net",  "Positive edge_net",      "signals_computed"),
    ("already_positioned", "  Ya posicionado",       "positive_edge_net"),
    ("risk_approved",      "  Risk approved",        "positive_edge_net"),
    ("risk_rejected",      "  Risk rejected",        "positive_edge_net"),
    ("risk_trimmed",       "  Risk trimmed (aprov.)", "risk_approved"),
    ("executed",           "Executed",               "risk_approved"),
    ("exited",             "Exited",                 "executed"),
]


def render_funnel(totals: dict[str, int], signals: list[dict], n_cycles: int) -> None:
    # Enriquecer con datos de trades cuando funnel_events no tenga esos campos
    totals = dict(totals)

    # Conteos derivados de la tabla de señales (si funnel_events está vacío o parcial)
    if not totals:
        totals["signals_computed"]   = len(signals)
        totals["passed_guards"]      = sum(1 for s in signals if s.get("guard_passed"))
        totals["positive_edge_net"]  = sum(1 for s in signals if (s.get("edge_net") or 0) > 0 and s.get("guard_passed"))
        # already_positioned no se puede derivar de signals (no deja huella en la tabla)
        totals["already_positioned"] = 0
        totals["risk_approved"]      = sum(1 for s in signals if (s.get("approved_size_usd") or 0) > 0)
        totals["risk_rejected"]      = sum(1 for s in signals if (s.get("approved_size_usd") or 0) == 0 and s.get("reject_reason"))
        totals["risk_trimmed"]       = sum(1 for s in signals if s.get("trim_reason"))
        totals["executed"]           = sum(1 for s in signals if s.get("trade_id"))

    # Añadir resolved/winners/losers desde señales en cualquier caso
    totals.setdefault("resolved", sum(1 for s in signals if s.get("outcome") is not None))
    totals.setdefault("winners",  sum(1 for s in signals if (s.get("pnl_usd") or 0) > 0 and s.get("trade_id")))
    totals.setdefault("losers",   sum(
        1 for s in signals
        if (s.get("pnl_usd") or 0) <= 0 and s.get("trade_id") and s.get("trade_status") == "closed"
    ))

    base = totals.get("markets_fetched") or totals.get("signals_computed") or 1

    rows: list[tuple[str, str, str | None]] = list(_PYRAMID) + [
        ("resolved", "  Resueltos",  "executed"),
        ("winners",  "    Ganadores", "resolved"),
        ("losers",   "    Perdedores", "resolved"),
    ]

    print(f"\n{BOLD}═══ A. PIRÁMIDE DE FUNNEL  ({n_cycles} ciclos acumulados) ═══{RESET}")
    print(f"{'Etapa':<28} {'Count':>8}  {'% prev':>8}  {'% total':>8}")
    print("─" * 58)

    for name, label, prev_name in rows:
        n = totals.get(name)
        if n is None:
            continue

        prev = totals.get(prev_name) if prev_name else base
        if not prev:
            prev = 1

        pct_prev  = n / prev
        pct_total = n / base

        # Colorear según stage
        if name in ("risk_rejected",):
            color = RED if pct_prev > 0.5 else YEL
        elif name in ("risk_trimmed",):
            color = YEL if pct_prev > 0.1 else DIM
        elif prev_name is None:
            color = RESET
        else:
            color = GRN if pct_prev > 0.15 else (YEL if pct_prev > 0.05 else RED)

        print(
            f"  {label:<26} {n:>8}  "
            f"{color}{_pct(pct_prev):>8}{RESET}  "
            f"{DIM}{_pct(pct_total):>8}{RESET}"
        )


# ─── SECCIÓN B: BREAKDOWNS DE RECHAZO / TRIM ─────────────────────────────────

def render_rejection_breakdown(
    rejects: list[tuple[str, int]],
    trims: list[tuple[str, int]],
    guard_fails: list[tuple[str, int]],
    signals: list[dict],
) -> None:
    n_actionable = sum(1 for s in signals if (s.get("edge_net") or 0) > 0 and s.get("guard_passed"))
    n_trimmed    = sum(1 for s in signals if (s.get("approved_size_usd") or 0) > 0 and s.get("trim_reason"))
    n_failed_guard = sum(1 for s in signals if not s.get("guard_passed"))

    print(f"\n{BOLD}═══ B1. RECHAZO DE GUARDS  ({n_failed_guard} señales) ═══{RESET}")
    if guard_fails:
        print(f"  {'Motivo':<30} {'Count':>6}  {'%':>7}  {'Barra'}")
        print("  " + "─" * 60)
        for reason, count in guard_fails:
            pct = count / n_failed_guard if n_failed_guard else 0
            print(f"  {reason:<30} {count:>6}  {_pct(pct):>7}  {_bar(pct)}")
    else:
        print(f"  {DIM}Sin datos de guard failures{RESET}")

    print(f"\n{BOLD}═══ B2. RECHAZO POR RISK ENGINE  ({n_actionable} señales accionables) ═══{RESET}")
    if rejects:
        print(f"  {'reject_reason':<30} {'Count':>6}  {'%':>7}  {'Barra'}")
        print("  " + "─" * 60)
        for reason, count in rejects:
            pct = count / n_actionable if n_actionable else 0
            print(f"  {reason:<30} {count:>6}  {_pct(pct):>7}  {_bar(pct)}")
    else:
        print(f"  {DIM}Sin rechazos registrados (o sin datos de sizing aún){RESET}")

    print(f"\n{BOLD}═══ B3. TRIM POR RISK CAPS  ({n_trimmed} trades aprobados con recorte) ═══{RESET}")
    if trims:
        print(f"  {'trim_reason':<30} {'Count':>6}  {'%':>7}  {'Barra'}")
        print("  " + "─" * 60)
        for reason, count in trims:
            pct = count / n_trimmed if n_trimmed else 0
            print(f"  {reason:<30} {count:>6}  {_pct(pct):>7}  {_bar(pct)}")
    else:
        print(f"  {DIM}Sin trims registrados{RESET}")


# ─── SECCIÓN C: COHORTES ─────────────────────────────────────────────────────

# Definición de cohorts: (campo, etiqueta, edges, labels)
COHORTS: list[tuple[str, str, list[float], list[str]]] = [
    (
        "spread_pct",
        "Spread %",
        [0.01, 0.02, 0.05],
        ["< 1%", "1–2%", "2–5%", "> 5%"],
    ),
    (
        "bid_depth_usd",
        "Bid depth (USD)",
        [500, 2_000, 10_000],
        ["< $500", "$500–2k", "$2k–10k", "> $10k"],
    ),
    (
        "hours_to_resolution",
        "Horas a resolución",
        [24, 168, 720],
        ["< 24h", "1–7d", "7–30d", "> 30d"],
    ),
    (
        "volume_24h",
        "Vol 24h (USD)",
        [1_000, 10_000, 100_000],
        ["< $1k", "$1k–10k", "$10k–100k", "> $100k"],
    ),
    (
        "open_interest",
        "Open interest (USD)",
        [5_000, 20_000, 100_000],
        ["< $5k", "$5k–20k", "$20k–100k", "> $100k"],
    ),
]


def _bucket(value: float | None, edges: list[float], labels: list[str]) -> str:
    if value is None:
        return "desconocido"
    for i, edge in enumerate(edges):
        if value < edge:
            return labels[i]
    return labels[-1]


def _cohort_stats(signals: list[dict], field: str, edges: list, labels: list, min_n: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        groups[_bucket(s.get(field), edges, labels)].append(s)

    rows = []
    for label in labels + ["desconocido"]:
        bucket = groups.get(label, [])
        if len(bucket) < min_n:
            continue

        n                = len(bucket)
        n_guard_pass     = sum(1 for s in bucket if s.get("guard_passed"))
        n_pos_edge       = sum(1 for s in bucket if (s.get("edge_net") or 0) > 0 and s.get("guard_passed"))
        n_approved       = sum(1 for s in bucket if (s.get("approved_size_usd") or 0) > 0)
        n_executed       = sum(1 for s in bucket if s.get("trade_id"))
        n_resolved       = sum(1 for s in bucket if s.get("outcome") is not None)
        n_winners        = sum(1 for s in bucket if (s.get("pnl_usd") or 0) > 0 and s.get("trade_id"))

        survival_rate    = n_pos_edge / n if n else None
        hit_rate         = n_winners / n_executed if n_executed else None

        pos_edges        = [s.get("edge_net") or 0 for s in bucket if (s.get("edge_net") or 0) > 0]
        avg_edge_net     = sum(pos_edges) / len(pos_edges) if pos_edges else None

        ev_vals          = [
            (s.get("edge_net") or 0) * (s.get("approved_size_usd") or 0)
            for s in bucket if (s.get("approved_size_usd") or 0) > 0
        ]
        avg_ev           = sum(ev_vals) / len(ev_vals) if ev_vals else None

        pnl_vals         = [s.get("pnl_usd") or 0 for s in bucket if s.get("trade_id") and s.get("trade_status") == "closed"]
        avg_pnl          = sum(pnl_vals) / len(pnl_vals) if pnl_vals else None

        fill_costs       = [s.get("fill_total_pct") or 0 for s in bucket if s.get("fill_total_pct")]
        avg_fill_cost    = sum(fill_costs) / len(fill_costs) if fill_costs else None

        rows.append({
            "label": label, "n": n, "n_guard_pass": n_guard_pass,
            "n_pos_edge": n_pos_edge, "n_approved": n_approved,
            "n_executed": n_executed, "n_resolved": n_resolved, "n_winners": n_winners,
            "survival_rate": survival_rate, "hit_rate": hit_rate,
            "avg_edge_net": avg_edge_net, "avg_ev": avg_ev,
            "avg_pnl": avg_pnl, "avg_fill_cost": avg_fill_cost,
        })

    return rows


def render_cohort_table(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"\n  {DIM}{label}: sin datos suficientes{RESET}")
        return

    print(f"\n{BOLD}── {label} ──{RESET}")
    hdr = f"  {'Bucket':<16} {'n':>6}  {'survival':>9}  {'hit rate':>9}  {'avg edge_net':>13}  {'avg EV$':>8}  {'avg fill%':>10}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    for r in rows:
        fill_str = _pct(r["avg_fill_cost"]) if r.get("avg_fill_cost") else "  n/a"
        print(
            f"  {r['label']:<16} "
            f"{r['n']:>6}  "
            f"{_color_rate(r['survival_rate']):>9}  "
            f"{_color_rate(r['hit_rate'], lo=0.45, hi=0.55):>9}  "
            f"{_color_num(r['avg_edge_net']):>13}  "
            f"{_color_num(r['avg_ev']):>8}  "
            f"{fill_str:>10}"
        )


def render_all_cohorts(signals: list[dict], min_n: int) -> None:
    print(f"\n{BOLD}═══ C. COHORTES ═══{RESET}")
    for field, label, edges, labels in COHORTS:
        rows = _cohort_stats(signals, field, edges, labels, min_n)
        render_cohort_table(label, rows)


# ─── SECCIÓN D: VEREDICTO ─────────────────────────────────────────────────────

# Etiquetas canónicas de veredicto (estables, fáciles de buscar en logs)
VERDICT_NO_SIGNAL           = "no_significant_signal"
VERDICT_DIES_AFTER_COSTS    = "signal_dies_after_costs"
VERDICT_THROTTLED_BY_RISK   = "signal_exists_but_risk_throttles_it"
VERDICT_COHORT_SPECIFIC     = "signal_survives_in_specific_cohorts"


def _compute_verdict(signals: list[dict], totals: dict[str, int]) -> tuple[str, list[str]]:
    """
    Reglas simples y transparentes. Sin estadística sofisticada.
    Devuelve (verdict_key, lista_de_observaciones).
    """
    n_total      = len(signals)
    n_pos_edge   = sum(1 for s in signals if (s.get("edge_net") or 0) > 0 and s.get("guard_passed"))
    n_approved   = sum(1 for s in signals if (s.get("approved_size_usd") or 0) > 0)
    n_executed   = sum(1 for s in signals if s.get("trade_id"))
    n_closed     = sum(1 for s in signals if s.get("trade_id") and s.get("trade_status") == "closed")
    n_winners    = sum(1 for s in signals if (s.get("pnl_usd") or 0) > 0 and s.get("trade_id"))
    n_risk_rej   = totals.get("risk_rejected", 0)
    n_risk_trim  = totals.get("risk_trimmed", 0)

    survival     = n_pos_edge / n_total if n_total else 0.0
    approval     = n_approved / n_pos_edge if n_pos_edge else 0.0
    hit_rate     = n_winners / n_closed if n_closed >= 5 else None

    obs: list[str] = []

    # ── Observaciones ──────────────────────────────────────────────────────
    obs.append(f"Señales totales: {n_total} | Positive edge_net: {n_pos_edge} ({_pct(survival).strip()})")
    obs.append(f"Risk aprobó: {n_approved} | Ejecutados: {n_executed} | Cerrados: {n_closed}")

    if n_risk_rej:
        obs.append(f"Risk rechazó {n_risk_rej} señales accionables.")
    if n_risk_trim:
        obs.append(f"Risk recortó el tamaño en {n_risk_trim} trades aprobados.")
    if hit_rate is not None:
        obs.append(f"Hit rate (cerrados): {_pct(hit_rate).strip()} con {n_closed} trades cerrados.")
    else:
        obs.append(f"Hit rate: datos insuficientes ({n_closed} trades cerrados < 5).")

    total_pnl = sum((s.get("pnl_usd") or 0) for s in signals if s.get("trade_id") and s.get("trade_status") == "closed")
    if n_closed:
        obs.append(f"PnL acumulado (trades cerrados): {_usd(total_pnl).strip()}")

    # ── Veredicto ──────────────────────────────────────────────────────────
    if survival < 0.02:
        verdict = VERDICT_DIES_AFTER_COSTS
        obs.append("→ El modelo no genera edge suficiente para cubrir el half-spread en la mayoría de mercados.")
        obs.append("  Acción: subir min_edge_raw (≥ 5%) o limitar a cohortes de spread < 1%.")

    elif n_pos_edge > 0 and n_approved == 0:
        verdict = VERDICT_THROTTLED_BY_RISK
        obs.append("→ Hay señales con edge positivo pero el risk engine las bloquea todas.")
        obs.append("  Acción: revisar caps (daily_deployment_pct, event_exposure_pct, max_concurrent).")

    elif n_risk_rej > n_approved and n_pos_edge > 0:
        verdict = VERDICT_THROTTLED_BY_RISK
        obs.append(f"→ El risk engine rechaza más señales ({n_risk_rej}) de las que aprueba ({n_approved}).")
        obs.append("  Revisa el breakdown B2 para identificar el cap más restrictivo.")

    elif survival >= 0.02 and n_closed < 5:
        verdict = VERDICT_COHORT_SPECIFIC
        obs.append("→ Hay señales que superan costos pero la muestra es demasiado pequeña para juzgar.")
        obs.append("  Acción: acumular ≥ 30 trades cerrados antes de evaluar hit rate.")

    elif hit_rate is not None and hit_rate < 0.45 and n_closed >= 5:
        verdict = VERDICT_NO_SIGNAL
        obs.append(f"→ Hit rate ({_pct(hit_rate).strip()}) por debajo del 45% con {n_closed} trades.")
        obs.append("  El edge pre-costo no se traduce en PnL. Revisar modelo de probabilidad.")

    elif survival >= 0.02 and approval >= 0.3:
        verdict = VERDICT_COHORT_SPECIFIC
        obs.append("→ El edge sobrevive en cohortes específicas. Ver tabla C para identificarlas.")
        obs.append("  Enfocar trades en buckets con survival ≥ 15% y avg_edge_net > fill_total_pct.")

    else:
        verdict = VERDICT_NO_SIGNAL
        obs.append("→ No hay evidencia clara de señal real en los datos disponibles.")
        obs.append("  Acción: revisar guards (demasiado laxos / restrictivos) y lógica del modelo.")

    return verdict, obs


def render_verdict(signals: list[dict], totals: dict[str, int]) -> None:
    verdict, obs = _compute_verdict(signals, totals)

    verdict_colors = {
        VERDICT_NO_SIGNAL:        RED,
        VERDICT_DIES_AFTER_COSTS: RED,
        VERDICT_THROTTLED_BY_RISK: YEL,
        VERDICT_COHORT_SPECIFIC:  GRN,
    }
    color = verdict_colors.get(verdict, RESET)

    print(f"\n{BOLD}═══ D. VEREDICTO ═══{RESET}")
    print(f"  {color}{BOLD}{verdict}{RESET}")
    print()
    for line in obs:
        prefix = "  " if line.startswith("→") or line.startswith("  ") else "  "
        print(f"{prefix}{line}")
    print()


# ─── SECCIÓN E: CAPITAL ──────────────────────────────────────────────────────

def render_capital_section(capital: dict) -> None:
    n          = capital["n_sized"]
    requested  = capital["total_requested"]
    approved   = capital["total_approved"]
    trimmed    = capital["total_trimmed"]
    by_reason  = capital["trim_by_reason"]  # list of (reason, n, usd)

    if n == 0:
        print(f"\n{BOLD}═══ E. CAPITAL (USD) ═══{RESET}")
        print(f"  {DIM}Sin señales que llegaron al risk engine todavía.{RESET}")
        return

    pct_approved = approved / requested if requested > 0 else 0.0
    pct_trimmed  = trimmed  / requested if requested > 0 else 0.0

    print(f"\n{BOLD}═══ E. CAPITAL (USD) — {n} señales evaluadas por risk engine ═══{RESET}")
    print(f"  {'Capital solicitado (requested):':<36} ${requested:>12,.2f}")
    print(f"  {'Capital aprobado   (approved):':<36} ${approved:>12,.2f}  ({_pct(pct_approved).strip()} del solicitado)")
    print(f"  {'Capital recortado  (trimmed):':<36} ${trimmed:>12,.2f}  ({_pct(pct_trimmed).strip()} del solicitado)")

    if by_reason:
        print(f"\n  {'trim_reason':<32} {'trades':>7}  {'USD recortado':>14}")
        print("  " + "─" * 58)
        for reason, n_r, usd_r in by_reason:
            pct_r = usd_r / trimmed if trimmed > 0 else 0.0
            print(f"  {reason:<32} {n_r:>7}  ${usd_r:>12,.2f}  ({_pct(pct_r).strip()})")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auditoría de funnel completa para probabilisticobot"
    )
    parser.add_argument("--db", required=True, help="Ruta a polybot.db")
    parser.add_argument("--min-n", type=int, default=3, help="Mínimo de señales por bucket de cohorte (default 3)")
    parser.add_argument("--limit", type=int, default=20_000, help="Señales máximas a cargar (default 20000)")
    parser.add_argument("--json", action="store_true", help="Salida JSON legible por máquina")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB no encontrada: {db_path}")
        sys.exit(1)

    async with aiosqlite.connect(str(db_path)) as db:
        totals        = await _load_funnel_totals(db)
        n_cycles      = await _load_n_cycles(db)
        capital_stats = await _load_capital_stats(db)
        signals       = await _load_signals(db, args.limit)
        rejects       = await _load_reject_breakdown(db)
        trims         = await _load_trim_breakdown(db)
        guard_fails   = await _load_guard_failures(db)

    if args.json:
        cohort_data: dict[str, Any] = {}
        for field, label, edges, labels in COHORTS:
            cohort_data[field] = _cohort_stats(signals, field, edges, labels, args.min_n)
        verdict, obs = _compute_verdict(signals, totals)
        print(json.dumps({
            "funnel_totals": totals,
            "n_signals": len(signals),
            "n_cycles": n_cycles,
            "capital": capital_stats,
            "reject_breakdown": [{"reason": r, "count": c} for r, c in rejects],
            "trim_breakdown":   [{"reason": r, "count": c} for r, c in trims],
            "cohorts": cohort_data,
            "verdict": verdict,
            "verdict_observations": obs,
        }, indent=2, default=str))
        return

    # ── Reporte de texto ──────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  FUNNEL AUDIT  —  {db_path.name}{RESET}")
    print(f"{DIM}  {len(signals)} señales cargadas  |  {n_cycles} ciclos{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")

    render_funnel(totals, signals, n_cycles)
    render_rejection_breakdown(rejects, trims, guard_fails, signals)
    render_all_cohorts(signals, args.min_n)
    render_capital_section(capital_stats)
    render_verdict(signals, totals)


if __name__ == "__main__":
    asyncio.run(main())
