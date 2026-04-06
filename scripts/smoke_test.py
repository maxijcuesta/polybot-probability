#!/usr/bin/env python3
"""
smoke_test.py — Verificación mínima del sistema.

Ejecuta un ciclo completo en paper mode con DB temporal y verifica
los invariantes de funnel documentados en cycle.py y STATUS.md.

Uso:
    python scripts/smoke_test.py

Exit 0 si todos los checks pasan. Exit 1 si alguno falla.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    import aiosqlite
except ImportError:
    print("FAIL  aiosqlite no instalado. Ejecuta: pip install aiosqlite")
    sys.exit(1)

from polybot.config import BotConfig
from polybot.jobs.cycle import BotCycle
from polybot import db as storage


# ─── HELPERS ──────────────────────────────────────────────────────────────────

_PASS = "\033[92mPASS\033[0m"
_FAIL = "\033[91mFAIL\033[0m"
_SKIP = "\033[93mSKIP\033[0m"


def _result(ok: bool | None, label: str, detail: str = "") -> bool | None:
    """Returns True (pass), False (fail), or None (skip)."""
    tag = _PASS if ok is True else (_SKIP if ok is None else _FAIL)
    suffix = f"  ({detail})" if detail else ""
    print(f"  {tag}  {label}{suffix}")
    return ok


# ─── CHECKS ───────────────────────────────────────────────────────────────────

def check_imports() -> bool:
    """Todos los módulos importan sin error."""
    ok = True
    modules = [
        ("polybot.config",                 "BotConfig"),
        ("polybot.models",                 "Signal, Trade, SizingDecision"),
        ("polybot.jobs.cycle",             "BotCycle"),
        ("polybot.risk_engine.sizer",      "RiskEngine"),
        ("polybot.signal_engine.engine",   "SignalEngine"),
        ("polybot.execution_engine.paper", "PaperExecutionEngine"),
        ("polybot.analytics.metrics",      "MetricsEngine"),
        ("polybot.market_discovery.fetcher", "MarketFetcher"),
    ]
    for mod, label in modules:
        try:
            __import__(mod)
            ok = _result(True, f"import {mod}") and ok
        except Exception as e:
            ok = _result(False, f"import {mod}", str(e)) and ok
    return ok


def check_config() -> bool:
    """BotConfig.defaults() carga sin error y tiene valores razonables."""
    try:
        cfg = BotConfig.defaults()
        assert cfg.risk.bankroll_usd > 0
        assert cfg.operation.paper_trade is True
        assert cfg.operation.dry_run is True
        return _result(True, "BotConfig.defaults() — paper=True, dry_run=True")
    except Exception as e:
        return _result(False, "BotConfig.defaults()", str(e))


async def run_cycle(db_path: str) -> dict:
    cfg = BotConfig.defaults()
    cfg.operation.db_path = db_path
    await storage.ensure_schema(db_path)
    cycle = BotCycle(cfg)
    return await cycle.run_once()


async def check_cycle(db_path: str) -> tuple[bool, dict]:
    """Un ciclo completo termina con status=ok o status=error (no excepción)."""
    try:
        result = await asyncio.wait_for(run_cycle(db_path), timeout=120)
        ok = result.get("status") in ("ok", "error")
        detail = f"status={result.get('status')}, markets={result.get('markets_fetched', 0)}"
        return _result(ok, "run_once() completa sin excepción", detail), result
    except asyncio.TimeoutError:
        # Timeout puede ocurrir si la API de Polymarket tarda mucho o no hay red.
        # Se marca como SKIP (None) para no bloquear CI en entornos sin red.
        return _result(None, "run_once() completó en < 120s", "timeout — sin red o API lenta"), {}
    except Exception as e:
        return _result(False, "run_once() completa sin excepción", str(e)), {}


async def check_db_schema(db_path: str) -> bool:
    """Las tablas requeridas existen en la DB."""
    required = {"trades", "signals", "funnel_events", "market_snapshots"}
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
        missing = required - tables
        ok = len(missing) == 0
        detail = f"tablas: {sorted(tables & required)}" if ok else f"faltan: {sorted(missing)}"
        return _result(ok, "schema — tablas requeridas existen", detail)
    except Exception as e:
        return _result(False, "schema — tablas requeridas existen", str(e))


async def check_funnel_invariants(db_path: str) -> bool:
    """
    Verifica invariantes de funnel contra la DB real.

    Invariantes documentados en cycle.py:
      1. already_positioned + risk_approved + risk_rejected = positive_edge_net
      2. executed <= risk_approved
      3. approved_size_usd <= requested_size_usd  (en tabla signals)
    """
    all_ok = True
    try:
        async with aiosqlite.connect(db_path) as db:
            # Totales acumulados por event_name
            async with db.execute(
                "SELECT event_name, SUM(event_count) FROM funnel_events GROUP BY event_name"
            ) as cur:
                totals: dict[str, int] = {r[0]: (r[1] or 0) for r in await cur.fetchall()}

            # Invariante 1: already_positioned + risk_approved + risk_rejected = positive_edge_net
            pos_edge   = totals.get("positive_edge_net", 0)
            approved   = totals.get("risk_approved", 0)
            rejected   = totals.get("risk_rejected", 0)
            positioned = totals.get("already_positioned", 0)
            lhs = positioned + approved + rejected
            inv1_ok = (lhs == pos_edge) or (pos_edge == 0 and lhs == 0)
            detail1 = f"already_positioned({positioned}) + risk_approved({approved}) + risk_rejected({rejected}) = {lhs}  vs positive_edge_net={pos_edge}"
            all_ok = _result(inv1_ok, "invariante 1 — funnel coherente", detail1) and all_ok

            # Invariante 2: executed <= risk_approved
            executed = totals.get("executed", 0)
            inv2_ok  = executed <= approved
            detail2  = f"executed={executed} <= risk_approved={approved}"
            all_ok = _result(inv2_ok, "invariante 2 — executed <= risk_approved", detail2) and all_ok

            # Invariante 3: approved_size_usd <= requested_size_usd (signals table)
            async with db.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE approved_size_usd IS NOT NULL
                  AND requested_size_usd IS NOT NULL
                  AND approved_size_usd > requested_size_usd + 0.01
                """
            ) as cur:
                row = await cur.fetchone()
            n_violations = row[0] if row else 0
            inv3_ok = n_violations == 0
            detail3 = f"{n_violations} violaciones en signals"
            all_ok = _result(inv3_ok, "invariante 3 — approved_size_usd <= requested_size_usd", detail3) and all_ok

    except Exception as e:
        all_ok = _result(False, "check_funnel_invariants", str(e)) and all_ok

    return all_ok


async def check_funnel_audit_loads(db_path: str) -> bool:
    """Las funciones de carga de funnel_audit.py no lanzan excepción."""
    try:
        audit_path = Path(__file__).parent / "funnel_audit.py"
        if not audit_path.exists():
            return _result(None, "funnel_audit.py — carga de funciones", "archivo no encontrado")

        import importlib.util
        spec = importlib.util.spec_from_file_location("funnel_audit", audit_path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        async with aiosqlite.connect(db_path) as db:
            totals = await mod._load_funnel_totals(db)
            n_cycles = await mod._load_n_cycles(db)
            capital = await mod._load_capital_stats(db)

        detail = f"totals_keys={len(totals)}, n_cycles={n_cycles}, n_sized={capital['n_sized']}"
        return _result(True, "funnel_audit.py — funciones de carga", detail)
    except Exception as e:
        return _result(False, "funnel_audit.py — funciones de carga", str(e))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("\n\033[1m━━━ SMOKE TEST — probabilisticobot ━━━\033[0m\n")

    failures = 0

    # 1. Imports
    print("\033[1m[1] Imports\033[0m")
    if not check_imports():
        failures += 1

    # 2. Config
    print("\n\033[1m[2] Configuración\033[0m")
    if not check_config():
        failures += 1

    # 3. Ciclo + DB (usa DB temporal para no contaminar la DB real)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "smoke.db")

        print("\n\033[1m[3] Schema de DB\033[0m")
        await storage.ensure_schema(db_path)
        if not await check_db_schema(db_path):
            failures += 1

        print("\n\033[1m[4] Ciclo completo (paper + dry_run)\033[0m")
        cycle_result_flag, cycle_result = await check_cycle(db_path)
        if cycle_result_flag is False:
            failures += 1

        cycle_ran = cycle_result.get("status") == "ok"

        if not cycle_ran:
            print(f"     \033[2mnota: ciclo no completó (timeout o error de red) — invariantes con DB vacía\033[0m")

        print("\n\033[1m[5] Invariantes de funnel\033[0m")
        if not await check_funnel_invariants(db_path):
            failures += 1

        print("\n\033[1m[6] funnel_audit.py — carga de datos\033[0m")
        if not await check_funnel_audit_loads(db_path):
            failures += 1

    # Resultado final
    print()
    if failures == 0:
        print("\033[92m\033[1m✓  Todos los checks pasaron.\033[0m\n")
        return 0
    else:
        print(f"\033[91m\033[1m✗  {failures} check(s) fallaron.\033[0m\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
