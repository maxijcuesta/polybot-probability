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
    RejectReason,
    Side,
    Signal,
    SizingDecision,
    Trade,
    TradeStatus,
    TrimReason,
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
        created_at TEXT,
        -- cohort fields for diagnostics / audit
        spread_pct REAL,
        volume_24h REAL,
        open_interest REAL,
        hours_to_resolution REAL,
        bid_depth_usd REAL,
        -- cost / funnel fields
        fill_total_pct REAL,
        requested_size_usd REAL,
        approved_size_usd REAL,
        reject_reason TEXT,
        trim_reason TEXT,
        risk_limited INTEGER,
        -- InefficiencyScorer output (saved for ALL guard-passed markets)
        final_trade_score REAL,
        inefficiency_score REAL,
        execution_score REAL,
        suggested_side TEXT,
        -- Score components (for cohort drill-down)
        microprice_gap REAL,
        spread_signal REAL,
        book_imbalance REAL,
        price_centrality REAL,
        vol_activity REAL,
        spread_cost REAL,
        staleness REAL,
        resolution_window REAL
    );

    CREATE TABLE IF NOT EXISTS funnel_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id TEXT NOT NULL,
        event_name TEXT NOT NULL,
        event_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
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
    """Create all tables if they don't exist, then apply migrations."""
    _ensure_dir(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await _migrate_schema(db)
        await db.commit()


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """
    Migraciones idempotentes para DBs existentes.

    1. Agrega columnas nuevas a `signals` si no existen (ALTER TABLE es seguro).
    2. Si `funnel_events` existe con el schema wide (columna markets_fetched),
       migra los datos al formato EAV y renombra la tabla vieja.
    """
    # ── Nuevas columnas en signals ─────────────────────────────────────────
    _signal_cols = [
        ("spread_pct", "REAL"),
        ("volume_24h", "REAL"),
        ("open_interest", "REAL"),
        ("hours_to_resolution", "REAL"),
        ("bid_depth_usd", "REAL"),
        ("fill_total_pct", "REAL"),
        ("requested_size_usd", "REAL"),
        ("approved_size_usd", "REAL"),
        ("reject_reason", "TEXT"),
        ("trim_reason", "TEXT"),
        ("risk_limited", "INTEGER"),
        # InefficiencyScorer columns — added when this migration runs
        ("final_trade_score", "REAL"),
        ("inefficiency_score", "REAL"),
        ("execution_score", "REAL"),
        ("suggested_side", "TEXT"),
        ("microprice_gap", "REAL"),
        ("spread_signal", "REAL"),
        ("book_imbalance", "REAL"),
        ("price_centrality", "REAL"),
        ("vol_activity", "REAL"),
        ("spread_cost", "REAL"),
        ("staleness", "REAL"),
        ("resolution_window", "REAL"),
    ]
    for col, typ in _signal_cols:
        try:
            await db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        except Exception:
            pass  # la columna ya existe — ok

    # ── Migración funnel_events: wide → EAV ───────────────────────────────
    #
    # Idempotencia garantizada de la siguiente forma:
    #   - Si funnel_events ya es EAV (no tiene "markets_fetched"): no hacer nada.
    #   - Si funnel_events es wide Y el backup no existe: migrar y renombrar.
    #   - Si funnel_events es wide Y el backup ya existe: la migración se ejecutó
    #     previamente pero falló a medias; no intentar el RENAME (fallaría de
    #     todos modos), solo recrear la tabla EAV con los datos del wide que aún
    #     está ahí, usando un nombre de backup con timestamp para no colisionar.
    async with db.execute("PRAGMA table_info(funnel_events)") as cur:
        cols = {row[1] for row in await cur.fetchall()}

    if "markets_fetched" in cols:
        # Schema wide detectado
        _wide_fields = [
            "markets_fetched", "passed_guards", "signals_computed",
            "positive_edge_net", "risk_approved", "executed", "exited",
        ]
        async with db.execute(
            "SELECT cycle_id, created_at, " + ", ".join(_wide_fields) + " FROM funnel_events"
        ) as cur:
            old_rows = await cur.fetchall()

        # Elegir nombre del backup que no colisione
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='funnel_events_wide_backup'"
        ) as cur:
            backup_exists = (await cur.fetchone()) is not None

        backup_name = (
            "funnel_events_wide_backup"
            if not backup_exists
            else f"funnel_events_wide_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        await db.execute(f"ALTER TABLE funnel_events RENAME TO {backup_name}")

        await db.execute("""
            CREATE TABLE funnel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        for row in old_rows:
            cycle_id, created_at = row[0], row[1]
            for i, field in enumerate(_wide_fields):
                count = row[2 + i] or 0
                await db.execute(
                    "INSERT INTO funnel_events (cycle_id, event_name, event_count, created_at) VALUES (?, ?, ?, ?)",
                    (cycle_id, field, count, created_at or ""),
                )


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
    features = signal.features
    spread_pct = float(features.spread_pct) if features and hasattr(features, "spread_pct") else None
    volume_24h = float(features.volume_24h) if features and hasattr(features, "volume_24h") else None
    open_interest = float(features.open_interest) if features and hasattr(features, "open_interest") else None
    hours_to_res = float(features.hours_to_resolution) if features and hasattr(features, "hours_to_resolution") else None
    bid_depth = float(features.bid_depth_usd) if features and hasattr(features, "bid_depth_usd") else None
    fill_total_pct = round(signal.costs.total, 6) if signal.costs else None

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO signals (
                signal_id, market_id, side, p_market, p_model, p_calibrated,
                edge_raw, edge_net, guard_passed, guard_failures, acted_on, created_at,
                spread_pct, volume_24h, open_interest, hours_to_resolution, bid_depth_usd,
                fill_total_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                spread_pct,
                volume_24h,
                open_interest,
                hours_to_res,
                bid_depth,
                fill_total_pct,
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


async def update_signal_score(db_path: str, signal_id: str, score: Any) -> None:
    """
    Persist InefficiencyScore fields for a signal.

    Called for every guard-passed market (not just top-N candidates) so
    cohort analysis can compare selected vs non-selected markets.

    Args:
        score: InefficiencyScore dataclass instance from scoring.scorer.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE signals SET
                final_trade_score = ?,
                inefficiency_score = ?,
                execution_score    = ?,
                suggested_side     = ?,
                microprice_gap     = ?,
                spread_signal      = ?,
                book_imbalance     = ?,
                price_centrality   = ?,
                vol_activity       = ?,
                spread_cost        = ?,
                staleness          = ?,
                resolution_window  = ?
            WHERE signal_id = ?
            """,
            (
                score.final_trade_score,
                score.inefficiency_score,
                score.execution_score,
                score.suggested_side,
                score.microprice_gap,
                score.spread_signal,
                score.book_imbalance,
                score.price_centrality,
                score.vol_activity,
                score.spread_cost,
                score.staleness,
                score.resolution_window,
                signal_id,
            ),
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

async def update_signal_sizing(
    db_path: str,
    signal_id: str,
    decision: SizingDecision,
) -> None:
    """
    Actualiza los campos de sizing en la fila de señal ya guardada.
    Llamado inmediatamente después de RiskEngine.size_position().

    Los enums RejectReason / TrimReason se persisten como su .value (str corto).
    """
    reject_val = decision.reject_reason.value if isinstance(decision.reject_reason, RejectReason) else decision.reject_reason
    trim_val   = decision.trim_reason.value   if isinstance(decision.trim_reason,   TrimReason)   else decision.trim_reason

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE signals
            SET requested_size_usd = ?,
                approved_size_usd  = ?,
                reject_reason      = ?,
                trim_reason        = ?,
                risk_limited       = ?,
                acted_on           = ?
            WHERE signal_id = ?
            """,
            (
                decision.requested_size_usd,
                decision.approved_size_usd,
                reject_val,
                trim_val,
                1 if decision.risk_limited else 0,
                1 if decision.approved else 0,
                signal_id,
            ),
        )
        await db.commit()


# ── Nombre canónico del evento de funnel → descripción legible ────────────────
FUNNEL_EVENT_NAMES: tuple[str, ...] = (
    "markets_fetched",
    "passed_guards",
    "signals_computed",
    "positive_edge_net",
    "already_positioned",  # accionables omitidos por posición existente en ese mercado
    "risk_approved",
    "risk_rejected",
    "risk_trimmed",        # subset de risk_approved con size recortado
    "executed",
    "exited",
)


async def save_funnel_events(
    db_path: str,
    cycle_id: str,
    counts: dict[str, int],
    created_at: str | None = None,
) -> None:
    """
    Persiste los contadores de funnel de un ciclo en formato EAV.

    Formato: una fila por event_name, para que agregar nuevos eventos
    no requiera cambios de schema.

    Args:
        cycle_id:    identificador del ciclo (uuid corto)
        counts:      dict { event_name → int }
        created_at:  ISO8601; si None usa utc_now
    """
    ts = created_at or _dt_to_str(datetime.now(timezone.utc))
    async with aiosqlite.connect(db_path) as db:
        for name, count in counts.items():
            await db.execute(
                "INSERT INTO funnel_events (cycle_id, event_name, event_count, created_at) VALUES (?, ?, ?, ?)",
                (cycle_id, name, count or 0, ts),
            )
        await db.commit()


async def load_funnel_totals(db_path: str) -> dict[str, int]:
    """
    Devuelve la suma acumulada de cada event_name en toda la historia.

    Útil para la pirámide del funnel audit.
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT event_name, SUM(event_count) FROM funnel_events GROUP BY event_name"
        ) as cur:
            rows = await cur.fetchall()
    return {row[0]: (row[1] or 0) for row in rows}


async def load_funnel_events_raw(
    db_path: str, limit: int = 500
) -> list[dict[str, Any]]:
    """Filas crudas de funnel_events, más recientes primero (para análisis de tendencia)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, cycle_id, event_name, event_count, created_at
            FROM funnel_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def load_signals_for_audit(db_path: str, limit: int = 10_000) -> list[dict[str, Any]]:
    """
    Load signals with all cohort and sizing fields for funnel audit.
    Joins with trades to include resolution outcome and PnL.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                s.signal_id,
                s.market_id,
                s.side,
                s.p_market,
                s.p_model,
                s.p_calibrated,
                s.edge_raw,
                s.edge_net,
                s.guard_passed,
                s.guard_failures,
                s.acted_on,
                s.created_at,
                s.spread_pct,
                s.volume_24h,
                s.open_interest,
                s.hours_to_resolution,
                s.bid_depth_usd,
                s.fill_total_pct,
                s.requested_size_usd,
                s.approved_size_usd,
                s.reject_reason,
                s.trim_reason,
                s.risk_limited,
                t.trade_id,
                t.status      AS trade_status,
                t.pnl_usd,
                t.pnl_pct,
                t.outcome,
                t.entry_price,
                t.exit_price,
                t.exit_reason
            FROM signals s
            LEFT JOIN trades t ON t.signal_id = s.signal_id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def load_cohort_data(db_path: str) -> list[dict[str, Any]]:
    """
    Load closed trades joined with their InefficiencyScore data.

    Used by scripts/cohort_report.py to answer:
    "Does final_trade_score correlate with better outcomes?"

    Returns:
        List of dicts with trade + score fields.
        Includes ALL guard-passed signals (acted_on=0 ones too) so the
        report can compare selected vs non-selected markets side-by-side.
        Rows without a final_trade_score are excluded (pre-scorer trades).
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                t.trade_id,
                t.market_id,
                t.side            AS trade_side,
                t.status,
                t.pnl_usd,
                t.pnl_pct,
                t.outcome,
                t.entry_price,
                t.exit_price,
                t.exit_reason,
                t.entry_size_usd,
                t.p_calibrated,
                s.signal_id,
                s.final_trade_score,
                s.inefficiency_score,
                s.execution_score,
                s.suggested_side,
                s.microprice_gap,
                s.spread_signal,
                s.book_imbalance,
                s.price_centrality,
                s.vol_activity,
                s.spread_cost,
                s.staleness,
                s.resolution_window,
                s.acted_on,
                s.spread_pct,
                s.hours_to_resolution
            FROM trades t
            JOIN signals s ON s.signal_id = t.signal_id
            WHERE t.status = 'closed'
              AND s.final_trade_score IS NOT NULL
            ORDER BY s.final_trade_score ASC
            """,
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def load_all_scored_signals(db_path: str, limit: int = 10_000) -> list[dict[str, Any]]:
    """
    Load all guard-passed signals that have a score (acted_on or not).

    Used by cohort_report to show the full scored universe, not just
    the trades that were executed.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                s.signal_id,
                s.market_id,
                s.side,
                s.guard_passed,
                s.acted_on,
                s.final_trade_score,
                s.inefficiency_score,
                s.execution_score,
                s.suggested_side,
                s.microprice_gap,
                s.spread_signal,
                s.book_imbalance,
                s.price_centrality,
                s.vol_activity,
                s.spread_cost,
                s.staleness,
                s.resolution_window,
                s.spread_pct,
                s.hours_to_resolution,
                s.created_at,
                t.trade_id,
                t.status      AS trade_status,
                t.pnl_usd,
                t.outcome,
                t.p_calibrated
            FROM signals s
            LEFT JOIN trades t ON t.signal_id = s.signal_id
            WHERE s.final_trade_score IS NOT NULL
            ORDER BY s.final_trade_score DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


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
