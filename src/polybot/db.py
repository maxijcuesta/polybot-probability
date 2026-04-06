"""
SQLite persistence layer for the trading bot.

Function-based API (not ORM). All queries parameterized.
Tables: trades, signals, market_snapshots, calibration_data
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    EntryReason,
    ExitReason,
    MarketSnapshot,
    Side,
    Signal,
    Trade,
    TradeStatus,
)

# ─── SCHEMA ───────────────────────────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL,
        signal_id TEXT,
        side TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        entry_price REAL,
        entry_size_usd REAL,
        entry_shares REAL,
        entry_time TEXT,
        entry_reason TEXT,
        p_model REAL,
        p_calibrated REAL,
        p_market_entry REAL,
        edge_raw REAL,
        edge_net REAL,
        exit_price REAL,
        exit_time TEXT,
        exit_reason TEXT,
        pnl_usd REAL,
        pnl_pct REAL,
        slippage_entry REAL DEFAULT 0,
        slippage_exit REAL DEFAULT 0,
        mae_usd REAL,
        mfe_usd REAL,
        outcome INTEGER,
        notes TEXT DEFAULT '',
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL,
        side TEXT NOT NULL,
        p_market REAL,
        p_model REAL,
        p_calibrated REAL,
        edge_raw REAL,
        edge_net REAL,
        guard_passed INTEGER,
        guard_failures TEXT,
        acted_on INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL,
        question TEXT,
        category TEXT,
        best_bid REAL,
        best_ask REAL,
        mid REAL,
        spread REAL,
        volume_24h REAL,
        open_interest REAL,
        last_trade_price REAL,
        fetched_at TEXT
    );

    CREATE TABLE IF NOT EXISTS calibration_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT NOT NULL,
        market_id TEXT,
        p_predicted REAL NOT NULL,
        outcome INTEGER,
        recorded_at TEXT,
        FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
    );
"""


def _dt_to_str(dt: datetime | None) -> str | None:
    """Convert datetime to ISO8601 string for storage."""
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    """Parse ISO8601 string to datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _ensure_dir(db_path: str) -> None:
    """Create parent directories for the DB file if needed."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)


# ─── SCHEMA MANAGEMENT ────────────────────────────────────────────────────────

async def ensure_schema(db_path: str) -> None:
    """Create all tables if they don't exist."""
    _ensure_dir(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


# ─── TRADES ───────────────────────────────────────────────────────────────────

async def save_trade(db_path: str, trade: Trade) -> None:
    """Insert or replace a trade record."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO trades (
                trade_id, market_id, signal_id, side, status,
                entry_price, entry_size_usd, entry_shares,
                entry_time, entry_reason,
                p_model, p_calibrated, p_market_entry, edge_raw, edge_net,
                exit_price, exit_time, exit_reason,
                pnl_usd, pnl_pct,
                slippage_entry, slippage_exit,
                mae_usd, mfe_usd, outcome, notes, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                trade.trade_id,
                trade.market_id,
                trade.signal_id,
                trade.side.value if isinstance(trade.side, Side) else trade.side,
                trade.status.value if isinstance(trade.status, TradeStatus) else trade.status,
                trade.entry_price,
                trade.entry_size_usd,
                trade.entry_shares,
                _dt_to_str(trade.entry_time),
                trade.entry_reason.value if isinstance(trade.entry_reason, EntryReason) else trade.entry_reason,
                trade.p_model,
                trade.p_calibrated,
                trade.p_market_entry,
                trade.edge_raw,
                trade.edge_net,
                trade.exit_price,
                _dt_to_str(trade.exit_time),
                trade.exit_reason.value if isinstance(trade.exit_reason, ExitReason) else (trade.exit_reason or None),
                trade.pnl_usd,
                trade.pnl_pct,
                trade.slippage_entry,
                trade.slippage_exit,
                trade.mae_usd,
                trade.mfe_usd,
                trade.outcome,
                trade.notes,
                _dt_to_str(trade.updated_at),
            ),
        )
        await db.commit()


async def update_trade_exit(
    db_path: str,
    trade_id: str,
    exit_data: dict[str, Any],
) -> None:
    """
    Update exit-related fields of a trade.

    exit_data keys (all optional):
        exit_price, exit_time, exit_reason, pnl_usd, pnl_pct,
        slippage_exit, mae_usd, mfe_usd, outcome, status, notes, updated_at
    """
    allowed = {
        "exit_price", "exit_time", "exit_reason", "pnl_usd", "pnl_pct",
        "slippage_exit", "mae_usd", "mfe_usd", "outcome", "status",
        "notes", "updated_at",
    }
    filtered = {k: v for k, v in exit_data.items() if k in allowed}
    if not filtered:
        return

    # Normalize datetime and enum values
    for key in ("exit_time", "updated_at"):
        if key in filtered and isinstance(filtered[key], datetime):
            filtered[key] = _dt_to_str(filtered[key])

    for key in ("exit_reason",):
        if key in filtered and isinstance(filtered[key], ExitReason):
            filtered[key] = filtered[key].value

    if "status" in filtered and isinstance(filtered["status"], TradeStatus):
        filtered["status"] = filtered["status"].value

    set_clause = ", ".join(f"{k} = ?" for k in filtered.keys())
    values = list(filtered.values()) + [trade_id]

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE trades SET {set_clause} WHERE trade_id = ?",
            values,
        )
        await db.commit()


def _row_to_trade(row: aiosqlite.Row) -> Trade:
    """Reconstruct a Trade object from a DB row."""
    (
        trade_id, market_id, signal_id, side, status,
        entry_price, entry_size_usd, entry_shares,
        entry_time, entry_reason,
        p_model, p_calibrated, p_market_entry, edge_raw, edge_net,
        exit_price, exit_time, exit_reason,
        pnl_usd, pnl_pct,
        slippage_entry, slippage_exit,
        mae_usd, mfe_usd, outcome, notes, updated_at,
    ) = row

    return Trade(
        trade_id=trade_id,
        market_id=market_id,
        signal_id=signal_id or "",
        side=Side(side),
        status=TradeStatus(status),
        entry_price=entry_price or 0.0,
        entry_size_usd=entry_size_usd or 0.0,
        entry_shares=entry_shares or 0.0,
        entry_time=_str_to_dt(entry_time) or datetime.now(timezone.utc),
        entry_reason=EntryReason(entry_reason) if entry_reason else EntryReason.EDGE_THRESHOLD,
        p_model=p_model or 0.0,
        p_calibrated=p_calibrated or 0.0,
        p_market_entry=p_market_entry or 0.0,
        edge_raw=edge_raw or 0.0,
        edge_net=edge_net or 0.0,
        exit_price=exit_price,
        exit_time=_str_to_dt(exit_time),
        exit_reason=ExitReason(exit_reason) if exit_reason else None,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        slippage_entry=slippage_entry or 0.0,
        slippage_exit=slippage_exit or 0.0,
        mae_usd=mae_usd,
        mfe_usd=mfe_usd,
        outcome=outcome,
        notes=notes or "",
        updated_at=_str_to_dt(updated_at) or datetime.now(timezone.utc),
    )


async def load_open_trades(db_path: str) -> list[Trade]:
    """Load all trades with status='open'."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT trade_id, market_id, signal_id, side, status,
                   entry_price, entry_size_usd, entry_shares,
                   entry_time, entry_reason,
                   p_model, p_calibrated, p_market_entry, edge_raw, edge_net,
                   exit_price, exit_time, exit_reason,
                   pnl_usd, pnl_pct,
                   slippage_entry, slippage_exit,
                   mae_usd, mfe_usd, outcome, notes, updated_at
            FROM trades
            WHERE status = 'open'
            ORDER BY entry_time ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_trade(tuple(row)) for row in rows]


async def load_all_trades(db_path: str, limit: int = 1000) -> list[Trade]:
    """Load all trades (open + closed), most recent first."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT trade_id, market_id, signal_id, side, status,
                   entry_price, entry_size_usd, entry_shares,
                   entry_time, entry_reason,
                   p_model, p_calibrated, p_market_entry, edge_raw, edge_net,
                   exit_price, exit_time, exit_reason,
                   pnl_usd, pnl_pct,
                   slippage_entry, slippage_exit,
                   mae_usd, mfe_usd, outcome, notes, updated_at
            FROM trades
            ORDER BY entry_time DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_trade(tuple(row)) for row in rows]


async def load_trade(db_path: str, trade_id: str) -> Trade | None:
    """Load a single trade by ID."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT trade_id, market_id, signal_id, side, status,
                   entry_price, entry_size_usd, entry_shares,
                   entry_time, entry_reason,
                   p_model, p_calibrated, p_market_entry, edge_raw, edge_net,
                   exit_price, exit_time, exit_reason,
                   pnl_usd, pnl_pct,
                   slippage_entry, slippage_exit,
                   mae_usd, mfe_usd, outcome, notes, updated_at
            FROM trades
            WHERE trade_id = ?
            """,
            (trade_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_trade(tuple(row))


# ─── SIGNALS ──────────────────────────────────────────────────────────────────

async def save_signal(db_path: str, signal: Signal, acted_on: bool = False) -> None:
    """Log a trading signal to the signals table."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO signals (
                signal_id, market_id, side, p_market, p_model, p_calibrated,
                edge_raw, edge_net, guard_passed, guard_failures, acted_on, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.signal_id,
                signal.market_id,
                signal.side.value if isinstance(signal.side, Side) else signal.side,
                signal.p_market,
                signal.p_model,
                signal.p_calibrated,
                signal.edge_raw,
                signal.edge_net,
                1 if signal.guard_result.passed else 0,
                json.dumps([f.value for f in signal.guard_result.failures]),
                1 if acted_on else 0,
                _dt_to_str(signal.created_at),
            ),
        )
        await db.commit()


async def mark_signal_acted_on(db_path: str, signal_id: str) -> None:
    """Update the acted_on flag for a signal."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE signals SET acted_on = 1 WHERE signal_id = ?",
            (signal_id,),
        )
        await db.commit()


# ─── MARKET SNAPSHOTS ─────────────────────────────────────────────────────────

async def save_market_snapshot(db_path: str, snapshot: MarketSnapshot) -> None:
    """Log market data snapshot."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO market_snapshots (
                market_id, question, category,
                best_bid, best_ask, mid, spread,
                volume_24h, open_interest, last_trade_price, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.market_id,
                snapshot.question,
                snapshot.category,
                snapshot.best_bid,
                snapshot.best_ask,
                snapshot.mid,
                snapshot.spread,
                snapshot.volume_24h,
                snapshot.open_interest,
                snapshot.last_trade_price,
                _dt_to_str(snapshot.fetched_at),
            ),
        )
        await db.commit()


# ─── CALIBRATION DATA ─────────────────────────────────────────────────────────

async def save_calibration_data(
    db_path: str,
    trade_id: str,
    market_id: str,
    p_predicted: float,
    outcome: int | None = None,
) -> None:
    """Record calibration data point (prediction + outcome) for calibrator fitting."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO calibration_data (trade_id, market_id, p_predicted, outcome, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                market_id,
                p_predicted,
                outcome,
                _dt_to_str(datetime.now(timezone.utc)),
            ),
        )
        await db.commit()


async def load_calibration_data(
    db_path: str,
    min_samples: int = 0,
) -> tuple[list[float], list[int]]:
    """
    Load calibration data where outcome is known.

    Returns (p_predicted_list, outcomes_list) for fitting calibrators.
    Only returns rows where outcome is not NULL.
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            SELECT p_predicted, outcome
            FROM calibration_data
            WHERE outcome IS NOT NULL
            ORDER BY recorded_at ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()

    p_list = [r[0] for r in rows]
    y_list = [r[1] for r in rows]
    return p_list, y_list


# ─── ANALYTICS / SUMMARY ──────────────────────────────────────────────────────

async def get_performance_summary(db_path: str) -> dict[str, Any]:
    """
    Compute a high-level performance summary from the trades table.

    Returns a dict with total trades, wins, losses, net PnL, hit rate, etc.
    Suitable for a quick status check.
    """
    async with aiosqlite.connect(db_path) as db:
        # Overall stats
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_trades,
                COALESCE(SUM(pnl_usd), 0) AS total_pnl_usd,
                COALESCE(AVG(pnl_usd), 0) AS avg_pnl_usd,
                COALESCE(MAX(pnl_usd), 0) AS best_trade_usd,
                COALESCE(MIN(pnl_usd), 0) AS worst_trade_usd
            FROM trades
            WHERE status = 'closed'
            """
        ) as cursor:
            row = await cursor.fetchone()

        total, wins, losses, open_count_closed, total_pnl, avg_pnl, best, worst = row or (0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0)

        # Count open trades separately (the aggregate query only covers closed)
        async with db.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ) as cursor:
            open_row = await cursor.fetchone()
        open_count = open_row[0] if open_row else 0

        hit_rate = wins / total if total and total > 0 else 0.0

        # Exposure
        async with db.execute(
            "SELECT COALESCE(SUM(entry_size_usd), 0.0) FROM trades WHERE status = 'open'"
        ) as cursor:
            exposure_row = await cursor.fetchone()
        current_exposure = exposure_row[0] if exposure_row else 0.0

        # Signal stats
        async with db.execute(
            """
            SELECT COUNT(*), SUM(acted_on)
            FROM signals
            """
        ) as cursor:
            sig_row = await cursor.fetchone()
        total_signals = sig_row[0] if sig_row else 0
        acted_signals = sig_row[1] if sig_row else 0

    return {
        "total_closed_trades": total,
        "wins": wins,
        "losses": losses,
        "open_trades": open_count,
        "hit_rate": round(hit_rate, 4),
        "total_pnl_usd": round(float(total_pnl), 2),
        "avg_pnl_per_trade_usd": round(float(avg_pnl), 2),
        "best_trade_usd": round(float(best), 2),
        "worst_trade_usd": round(float(worst), 2),
        "current_exposure_usd": round(float(current_exposure), 2),
        "total_signals_generated": total_signals,
        "signals_acted_on": acted_signals,
        "signal_conversion_rate": round(acted_signals / total_signals, 4) if total_signals else 0.0,
    }
