#!/usr/bin/env python3
"""
Paper Trading Report — Análisis completo de operaciones simuladas.

Consulta la DB de SQLite y muestra:
  - Resumen ejecutivo: PnL total, win rate, expectancy
  - Posiciones abiertas: exposición actual, PnL flotante
  - Posiciones cerradas: histórico con razones de salida
  - Breakdown por tipo de señal (qué detector gana más)
  - PnL diario agregado
  - Oportunidades: tasa de conversión signal → trade

Uso:
  python scripts/paper_report.py
  python scripts/paper_report.py --db ./data/polybot.db
  python scripts/paper_report.py --days 7   # últimos 7 días
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── COLORES ANSI ─────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    GREY   = "\033[90m"
    WHITE  = "\033[97m"

def green(s: str) -> str:  return f"{C.GREEN}{s}{C.RESET}"
def red(s: str) -> str:    return f"{C.RED}{s}{C.RESET}"
def yellow(s: str) -> str: return f"{C.YELLOW}{s}{C.RESET}"
def cyan(s: str) -> str:   return f"{C.CYAN}{s}{C.RESET}"
def bold(s: str) -> str:   return f"{C.BOLD}{s}{C.RESET}"
def grey(s: str) -> str:   return f"{C.GREY}{s}{C.RESET}"

def pnl_color(val: float) -> str:
    """Colorea un valor de PnL: verde si positivo, rojo si negativo."""
    s = f"{val:+.2f}"
    return green(s) if val >= 0 else red(s)

def fmt_time(ts: float | None) -> str:
    """Convierte unix timestamp a string legible."""
    if ts is None:
        return grey("—")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")

def fmt_pct(val: float | None, decimals: int = 1) -> str:
    if val is None:
        return grey("—")
    s = f"{val:+.{decimals}f}%"
    return green(s) if val >= 0 else red(s)

def sep(width: int = 72, char: str = "─") -> str:
    return grey(char * width)


# ─── QUERIES ──────────────────────────────────────────────────────────────────

async def run_report(db_path: str, days: int | None = None) -> None:
    import aiosqlite

    since_ts: float | None = None
    if days:
        import time
        since_ts = time.time() - days * 86400

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # ── 0. Verificar que hay datos ──────────────────────────────────────
        async with db.execute("SELECT COUNT(*) FROM positions") as cur:
            total_pos = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM markets") as cur:
            total_markets = (await cur.fetchone())[0]

        print()
        print(bold("═" * 72))
        print(bold(f"  📊  PAPER TRADING REPORT  —  probabilisticobot"))
        print(bold("═" * 72))
        if since_ts:
            print(grey(f"  Período: últimos {days} días  |  DB: {db_path}"))
        else:
            print(grey(f"  Período: todo el histórico  |  DB: {db_path}"))
        print(grey(f"  Mercados en DB: {total_markets}  |  Posiciones totales: {total_pos}"))
        print()

        if total_pos == 0:
            print(yellow("  ⚠️  No hay posiciones en la DB todavía."))
            print(yellow("     Arranca el bot con: python -m app.main"))
            print(yellow("     y espera a que detecte señales.\n"))
            return

        # ── 1. POSICIONES ABIERTAS ──────────────────────────────────────────
        open_query = """
            SELECT p.id, p.condition_id, p.outcome, p.side,
                   p.size, p.avg_entry_price, p.cost_basis_usdc,
                   p.current_price, p.unrealized_pnl_usdc, p.unrealized_pnl_pct,
                   p.signal_type, p.opened_at,
                   p.max_pnl_pct, p.min_pnl_pct,
                   p.trailing_stop_active, p.trailing_stop_price,
                   m.question
              FROM positions p
              LEFT JOIN markets m USING (condition_id)
             WHERE p.status = 'open'
               AND p.paper_mode = 1
        """
        if since_ts:
            open_query += f" AND p.opened_at >= {since_ts}"
        open_query += " ORDER BY p.opened_at DESC"

        async with db.execute(open_query) as cur:
            open_positions = await cur.fetchall()

        print(sep())
        print(bold(f"  📂  POSICIONES ABIERTAS  ({len(open_positions)})"))
        print(sep())

        if not open_positions:
            print(grey("  Sin posiciones abiertas en este momento.\n"))
        else:
            total_exposure = sum(r["cost_basis_usdc"] for r in open_positions)
            total_unrealized = sum(r["unrealized_pnl_usdc"] for r in open_positions)

            print(f"  Exposición total:   {cyan(f'{total_exposure:.2f} USDC')}")
            print(f"  PnL flotante total: {pnl_color(total_unrealized)} USDC")
            print()

            header = f"  {'#':>4}  {'Mercado':<32}  {'Lado':>4}  {'Cost':>7}  {'PnL $':>8}  {'PnL%':>7}  {'Señal':<12}  {'Abierta'}"
            print(grey(header))
            print(grey("  " + "─" * 68))

            for r in open_positions:
                question = (r["question"] or r["condition_id"])[:30]
                trail = " 🎯" if r["trailing_stop_active"] else ""
                print(
                    f"  {r['id']:>4}  {question:<32}  "
                    f"{r['side']:>4}  "
                    f"{r['cost_basis_usdc']:>6.2f}  "
                    f"{pnl_color(r['unrealized_pnl_usdc']):>17}  "
                    f"{fmt_pct(r['unrealized_pnl_pct']):>16}  "
                    f"{r['signal_type']:<12}  "
                    f"{fmt_time(r['opened_at'])}"
                    f"{trail}"
                )
            print()

        # ── 2. POSICIONES CERRADAS ──────────────────────────────────────────
        closed_query = """
            SELECT p.id, p.condition_id, p.outcome, p.side,
                   p.cost_basis_usdc, p.signal_type,
                   e.exit_reason, e.realized_pnl_usdc, e.realized_pnl_pct,
                   e.hold_duration_hours, e.exit_price, e.exited_at,
                   e.mae_pct, e.mfe_pct,
                   m.question
              FROM positions p
              JOIN exits e ON e.position_id = p.id
              LEFT JOIN markets m USING (condition_id)
             WHERE p.status = 'closed'
               AND p.paper_mode = 1
        """
        if since_ts:
            closed_query += f" AND e.exited_at >= {since_ts}"
        closed_query += " ORDER BY e.exited_at DESC LIMIT 30"

        async with db.execute(closed_query) as cur:
            closed_positions = await cur.fetchall()

        # ── 3. ESTADÍSTICAS GENERALES ───────────────────────────────────────
        stats_query = """
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN e.realized_pnl_usdc > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN e.realized_pnl_usdc <= 0 THEN 1 ELSE 0 END) as losers,
                SUM(e.net_pnl_usdc) as total_net_pnl,
                AVG(e.realized_pnl_usdc) as avg_pnl,
                AVG(CASE WHEN e.realized_pnl_usdc > 0 THEN e.realized_pnl_usdc END) as avg_win,
                AVG(CASE WHEN e.realized_pnl_usdc <= 0 THEN e.realized_pnl_usdc END) as avg_loss,
                MAX(e.realized_pnl_usdc) as best_trade,
                MIN(e.realized_pnl_usdc) as worst_trade,
                AVG(e.hold_duration_hours) as avg_hold_hours
              FROM positions p
              JOIN exits e ON e.position_id = p.id
             WHERE p.paper_mode = 1
        """
        if since_ts:
            stats_query += f" AND e.exited_at >= {since_ts}"

        async with db.execute(stats_query) as cur:
            stats = await cur.fetchone()

        print(sep())
        print(bold("  📈  RESUMEN EJECUTIVO"))
        print(sep())

        total = stats["total_trades"] or 0
        winners = stats["winners"] or 0
        losers = stats["losers"] or 0
        net_pnl = stats["total_net_pnl"] or 0.0
        avg_win = stats["avg_win"] or 0.0
        avg_loss = stats["avg_loss"] or 0.0
        best = stats["best_trade"] or 0.0
        worst = stats["worst_trade"] or 0.0
        avg_hold = stats["avg_hold_hours"] or 0.0

        if total == 0:
            print(grey("  Sin trades cerrados todavía.\n"))
        else:
            win_rate = (winners / total * 100) if total > 0 else 0.0
            profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
            expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

            print(f"  Trades cerrados:   {bold(str(total))}   ({winners} ganadores / {losers} perdedores)")
            print(f"  Win rate:          {bold(fmt_pct(win_rate, 1))}")
            print(f"  PnL neto total:    {bold(pnl_color(net_pnl))} USDC")
            print(f"  Expectancy:        {pnl_color(expectancy)} USDC/trade")
            print(f"  Profit factor:     {bold(f'{profit_factor:.2f}')}")
            print(f"  Mejor trade:       {green(f'+{best:.2f} USDC')}")
            print(f"  Peor trade:        {red(f'{worst:.2f} USDC')}")
            print(f"  Avg. win:          {green(f'+{avg_win:.2f} USDC')}")
            print(f"  Avg. loss:         {red(f'{avg_loss:.2f} USDC')}")
            print(f"  Avg. duración:     {avg_hold:.1f} horas")
            print()

        # ── 4. ÚLTIMAS POSICIONES CERRADAS ─────────────────────────────────
        print(sep())
        print(bold(f"  🔒  ÚLTIMAS POSICIONES CERRADAS  ({len(closed_positions)})"))
        print(sep())

        if not closed_positions:
            print(grey("  Sin cierres todavía.\n"))
        else:
            hdr = f"  {'#':>4}  {'Mercado':<28}  {'Cost':>7}  {'PnL $':>8}  {'PnL%':>7}  {'Razón':<22}  {'Hrs':>5}  {'Cerrada'}"
            print(grey(hdr))
            print(grey("  " + "─" * 70))
            for r in closed_positions:
                question = (r["question"] or r["condition_id"])[:26]
                print(
                    f"  {r['id']:>4}  {question:<28}  "
                    f"{r['cost_basis_usdc']:>6.2f}  "
                    f"{pnl_color(r['realized_pnl_usdc']):>17}  "
                    f"{fmt_pct(r['realized_pnl_pct']):>16}  "
                    f"{r['exit_reason']:<22}  "
                    f"{r['hold_duration_hours']:>5.1f}  "
                    f"{fmt_time(r['exited_at'])}"
                )
            print()

        # ── 5. BREAKDOWN POR TIPO DE SEÑAL ─────────────────────────────────
        signal_query = """
            SELECT
                p.signal_type,
                COUNT(*) as trades,
                SUM(CASE WHEN e.realized_pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
                SUM(e.net_pnl_usdc) as net_pnl,
                AVG(e.realized_pnl_usdc) as avg_pnl,
                AVG(e.hold_duration_hours) as avg_hold
              FROM positions p
              JOIN exits e ON e.position_id = p.id
             WHERE p.paper_mode = 1
        """
        if since_ts:
            signal_query += f" AND e.exited_at >= {since_ts}"
        signal_query += " GROUP BY p.signal_type ORDER BY net_pnl DESC"

        async with db.execute(signal_query) as cur:
            by_signal = await cur.fetchall()

        if by_signal and total > 0:
            print(sep())
            print(bold("  🎯  BREAKDOWN POR TIPO DE SEÑAL"))
            print(sep())
            hdr = f"  {'Señal':<22}  {'Trades':>6}  {'Win%':>6}  {'Net PnL':>10}  {'Avg':>8}  {'Avg Hrs':>8}"
            print(grey(hdr))
            print(grey("  " + "─" * 65))
            for r in by_signal:
                wr = r["wins"] / r["trades"] * 100 if r["trades"] > 0 else 0
                print(
                    f"  {r['signal_type']:<22}  "
                    f"{r['trades']:>6}  "
                    f"{fmt_pct(wr, 0):>15}  "
                    f"{pnl_color(r['net_pnl']):>19}  "
                    f"{pnl_color(r['avg_pnl']):>17}  "
                    f"{r['avg_hold']:>7.1f}h"
                )
            print()

        # ── 6. RAZONES DE SALIDA ───────────────────────────────────────────
        exit_reason_query = """
            SELECT
                e.exit_reason,
                COUNT(*) as count,
                SUM(e.net_pnl_usdc) as net_pnl,
                AVG(e.realized_pnl_usdc) as avg_pnl
              FROM exits e
              JOIN positions p ON e.position_id = p.id
             WHERE p.paper_mode = 1
        """
        if since_ts:
            exit_reason_query += f" AND e.exited_at >= {since_ts}"
        exit_reason_query += " GROUP BY e.exit_reason ORDER BY count DESC"

        async with db.execute(exit_reason_query) as cur:
            by_reason = await cur.fetchall()

        if by_reason and total > 0:
            print(sep())
            print(bold("  🚪  RAZONES DE SALIDA"))
            print(sep())
            hdr = f"  {'Razón':<28}  {'Count':>5}  {'Net PnL':>10}  {'Avg PnL':>10}"
            print(grey(hdr))
            print(grey("  " + "─" * 58))
            for r in by_reason:
                print(
                    f"  {r['exit_reason']:<28}  "
                    f"{r['count']:>5}  "
                    f"{pnl_color(r['net_pnl']):>19}  "
                    f"{pnl_color(r['avg_pnl']):>19}"
                )
            print()

        # ── 7. PNL DIARIO ──────────────────────────────────────────────────
        daily_query = """
            SELECT date, trades_count, winners, losers,
                   net_pnl_usdc, win_rate_pct, expectancy_usdc
              FROM pnl
             ORDER BY date DESC
             LIMIT 14
        """
        async with db.execute(daily_query) as cur:
            daily = await cur.fetchall()

        if daily:
            print(sep())
            print(bold("  📅  PNL DIARIO (últimos 14 días)"))
            print(sep())
            hdr = f"  {'Fecha':<12}  {'Trades':>6}  {'W/L':>6}  {'Win%':>6}  {'Net PnL':>10}  {'Expectancy':>11}"
            print(grey(hdr))
            print(grey("  " + "─" * 60))
            for r in daily:
                wl = f"{r['winners']}/{r['losers']}"
                print(
                    f"  {r['date']:<12}  "
                    f"{r['trades_count']:>6}  "
                    f"{wl:>6}  "
                    f"{fmt_pct(r['win_rate_pct'], 0):>15}  "
                    f"{pnl_color(r['net_pnl_usdc']):>19}  "
                    f"{pnl_color(r['expectancy_usdc']):>20}"
                )
            print()

        # ── 8. OPORTUNIDADES (conversión) ──────────────────────────────────
        opp_query = """
            SELECT
                status,
                COUNT(*) as count,
                AVG(edge_pct) as avg_edge
              FROM opportunities
        """
        if since_ts:
            opp_query += f" WHERE detected_at >= {since_ts}"
        opp_query += " GROUP BY status"

        async with db.execute(opp_query) as cur:
            opps = {r["status"]: r for r in await cur.fetchall()}

        if opps:
            print(sep())
            print(bold("  🔍  FUNNEL DE OPORTUNIDADES"))
            print(sep())
            total_opps = sum(r["count"] for r in opps.values())
            for status, r in sorted(opps.items(), key=lambda x: -x[1]["count"]):
                pct = r["count"] / total_opps * 100 if total_opps > 0 else 0
                bar_len = int(pct / 2)
                bar = "█" * bar_len + "░" * (50 - bar_len)
                edge_str = f"  avg edge={r['avg_edge']:.1f}%" if r["avg_edge"] else ""
                print(f"  {status:<12}  {r['count']:>4}  ({pct:5.1f}%)  {grey(bar[:30])}{edge_str}")
            print()

        # ── FOOTER ─────────────────────────────────────────────────────────
        print(sep())
        print(grey("  💡  Para más detalle, abre la DB con:"))
        print(grey(f"      sqlite3 {db_path}"))
        print(grey("      SELECT * FROM exits ORDER BY exited_at DESC LIMIT 20;"))
        print(bold("═" * 72))
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trading Report")
    parser.add_argument(
        "--db",
        default="./data/polybot.db",
        help="Ruta al archivo SQLite (default: ./data/polybot.db)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Mostrar solo los últimos N días (default: todo el histórico)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(red(f"\n❌ DB no encontrada: {db_path}"))
        print(yellow("   Arranca el bot primero con: python -m app.main\n"))
        sys.exit(1)

    asyncio.run(run_report(str(db_path), days=args.days))


if __name__ == "__main__":
    main()
