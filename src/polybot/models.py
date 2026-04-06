from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ─── ENUMS ────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MAX_HOLD = "max_hold"
    EDGE_FLIP = "edge_flip"
    TRAILING_STOP = "trailing_stop"
    MANUAL = "manual"
    RESOLUTION = "resolution"


class EntryReason(str, Enum):
    EDGE_THRESHOLD = "edge_threshold"
    KELLY_SIGNAL = "kelly_signal"


class GuardFailReason(str, Enum):
    LOW_VOLUME = "low_volume"
    LOW_OPEN_INTEREST = "low_open_interest"
    HIGH_SPREAD = "high_spread"
    LOW_DEPTH = "low_depth"
    STALE_MARKET = "stale_market"
    INVALID_PRICE = "invalid_price"
    TOO_CLOSE_RESOLUTION = "too_close_resolution"
    TOO_FAR_RESOLUTION = "too_far_resolution"


class RejectReason(str, Enum):
    """Motivos estables por los que el risk engine rechaza un trade completo."""
    NO_EDGE                  = "no_edge"
    DAILY_LOSS_LIMIT         = "daily_loss_limit"
    MAX_DRAWDOWN             = "max_drawdown"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    DAILY_CAP_EXHAUSTED      = "daily_cap_exhausted"
    EVENT_CAP_EXHAUSTED      = "event_cap_exhausted"
    NO_PORTFOLIO_ROOM        = "no_portfolio_room"
    BELOW_MIN_SIZE           = "below_min_size"


class TrimReason(str, Enum):
    """Motivos estables por los que el trade fue aprobado pero con tamaño reducido."""
    PORTFOLIO_EXPOSURE_CAP = "portfolio_exposure_cap"
    DAILY_CAP              = "trimmed_by_daily_cap"
    EVENT_CAP              = "trimmed_by_event_cap"
    TRADE_CAP              = "trimmed_by_trade_cap"


# ─── MARKET DATA ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass(slots=True)
class Orderbook:
    bids: list[OrderbookLevel]  # buy YES orders (sorted desc)
    asks: list[OrderbookLevel]  # sell YES orders (sorted asc)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def bid_depth_usd(self) -> float:
        return sum(l.price * l.size for l in self.bids[:5])

    @property
    def ask_depth_usd(self) -> float:
        return sum(l.price * l.size for l in self.asks[:5])


@dataclass(slots=True)
class MarketSnapshot:
    """Raw market data from Polymarket API."""
    market_id: str
    condition_id: str
    question: str
    category: str
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    volume_24h: float
    volume_total: float
    open_interest: float
    last_trade_price: float
    last_trade_time: datetime | None
    resolution_time: datetime | None
    active: bool
    orderbook: Orderbook | None = None
    fetched_at: datetime = field(default_factory=utc_now)

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else 1.0

    @property
    def hours_to_resolution(self) -> float | None:
        if self.resolution_time is None:
            return None
        delta = self.resolution_time - utc_now()
        return delta.total_seconds() / 3600

    @property
    def hours_since_last_trade(self) -> float | None:
        if self.last_trade_time is None:
            return None
        delta = utc_now() - self.last_trade_time
        return delta.total_seconds() / 3600


# ─── FEATURE VECTOR ───────────────────────────────────────────────────────────

@dataclass(slots=True)
class MarketFeatures:
    """Derived features computed from MarketSnapshot."""
    market_id: str
    # Price features
    mid_price: float
    spread_pct: float
    bid_depth_usd: float
    ask_depth_usd: float
    depth_imbalance: float      # (bid - ask) / (bid + ask)
    # Volume features
    volume_24h: float
    open_interest: float
    volume_oi_ratio: float      # volume / OI (activity metric)
    # Time features
    hours_to_resolution: float
    hours_since_last_trade: float
    # Market structure
    is_binary: bool             # YES/NO binary market
    computed_at: datetime = field(default_factory=utc_now)

    def to_vector(self) -> list[float]:
        """Numeric feature vector for ML models."""
        return [
            self.mid_price,
            self.spread_pct,
            self.bid_depth_usd,
            self.ask_depth_usd,
            self.depth_imbalance,
            self.volume_24h,
            self.open_interest,
            self.volume_oi_ratio,
            self.hours_to_resolution,
            self.hours_since_last_trade,
        ]


# ─── PROBABILITY MODEL OUTPUT ────────────────────────────────────────────────

@dataclass(slots=True)
class ModelPrediction:
    """Raw output from the probability model."""
    market_id: str
    p_yes: float          # model's P(YES)
    p_no: float           # model's P(NO) = 1 - p_yes
    confidence: float     # 0-1, model confidence
    model_type: str
    computed_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class CalibratedPrediction:
    """Calibrated probability output."""
    market_id: str
    p_yes_raw: float
    p_yes_calibrated: float
    calibration_method: str
    n_samples_used: int   # how many historical trades used for calibration
    computed_at: datetime = field(default_factory=utc_now)


# ─── SIGNAL ───────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class GuardResult:
    passed: bool
    failures: list[GuardFailReason] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CostEstimate:
    taker_fee: float
    maker_fee: float
    slippage: float
    gas: float

    @property
    def total(self) -> float:
        return self.taker_fee + self.maker_fee + self.slippage + self.gas


@dataclass(slots=True)
class Signal:
    """A trading signal — the core output of the signal engine."""
    signal_id: str
    market_id: str
    side: Side
    # Prices
    p_market: float          # implied probability from market
    p_model: float           # raw model probability
    p_calibrated: float      # calibrated probability
    # Edge decomposition
    edge_raw: float          # p_calibrated - p_market
    costs: CostEstimate
    edge_net: float          # edge_raw - costs.total
    # Context
    features: MarketFeatures
    guard_result: GuardResult
    entry_reason: EntryReason
    created_at: datetime = field(default_factory=utc_now)

    @property
    def is_actionable(self) -> bool:
        return self.guard_result.passed and self.edge_net > 0


@dataclass(slots=True)
class SizeResult:
    """Position sizing output."""
    signal_id: str
    size_usd: float
    size_shares: float
    entry_price: float
    kelly_fraction_used: float
    reasoning: str


@dataclass(slots=True)
class SizingDecision:
    """
    Siempre devuelto por RiskEngine.size_position() — aprobado o rechazado —
    con contexto completo para trazabilidad de funnel.

    Reemplaza el patrón SizeResult | None.
    Los motivos usan RejectReason / TrimReason (enums estables, fáciles de agrupar).
    """
    approved: bool
    signal_id: str

    # ── Tamaños ────────────────────────────────────────────────────────────
    requested_size_usd: float = 0.0      # tamaño Kelly antes de cualquier cap
    approved_size_usd: float = 0.0       # tamaño final (0.0 si rechazado)

    # ── Motivos (enums: valores cortos, estables, agrupables) ──────────────
    reject_reason: RejectReason | None = None   # por qué fue rechazado
    trim_reason: TrimReason | None = None       # por qué fue recortado (si aplica)
    risk_limited: bool = False                  # True si algún cap actuó
    min_size_blocked: bool = False              # True si el rechazo fue por tamaño mínimo

    # ── Snapshot de caps (estado en el momento del sizing) ─────────────────
    daily_cap_remaining_usd: float | None = None   # cuánto quedaba del cap diario
    event_cap_remaining_usd: float | None = None   # cuánto quedaba del cap por evento

    # ── Contexto Kelly (para auditar la lógica de sizing) ─────────────────
    kelly_fraction_raw: float | None = None      # Kelly f puro antes del multiplicador
    kelly_fraction_applied: float | None = None  # después de kelly_fraction y trade cap
    bankroll_snapshot_usd: float | None = None   # bankroll en el momento del sizing
    max_trade_cap_usd: float | None = None       # cap máximo por trade (config)

    # ── Datos de posición (solo cuando approved=True) ──────────────────────
    size_shares: float = 0.0
    entry_price: float = 0.0
    reasoning: str = ""

    def to_size_result(self) -> "SizeResult | None":
        """Convierte a SizeResult para uso downstream. None si rechazado."""
        if not self.approved:
            return None
        return SizeResult(
            signal_id=self.signal_id,
            size_usd=self.approved_size_usd,
            size_shares=self.size_shares,
            entry_price=self.entry_price,
            kelly_fraction_used=self.kelly_fraction_applied or 0.0,
            reasoning=self.reasoning,
        )


# ─── TRADE / POSITION ────────────────────────────────────────────────────────

@dataclass(slots=True)
class Trade:
    """A complete trade record (entry + exit)."""
    trade_id: str
    market_id: str
    signal_id: str
    side: Side
    status: TradeStatus
    # Entry
    entry_price: float
    entry_size_usd: float
    entry_shares: float
    entry_time: datetime
    entry_reason: EntryReason
    # Model info at entry
    p_model: float
    p_calibrated: float
    p_market_entry: float
    edge_raw: float
    edge_net: float
    # Exit (filled when closed)
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: ExitReason | None = None
    # PnL
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    # Execution quality
    slippage_entry: float = 0.0
    slippage_exit: float = 0.0
    # Risk metrics (MAE/MFE in USD)
    mae_usd: float | None = None   # max adverse excursion
    mfe_usd: float | None = None   # max favorable excursion
    # Outcome for Brier/log-loss (1=YES won, 0=NO won, None=unresolved)
    outcome: int | None = None
    # Misc
    notes: str = ""
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PortfolioState:
    """Current portfolio state."""
    open_trades: list[Trade]
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_exposure_usd: float
    bankroll_usd: float
    daily_pnl_usd: float
    peak_bankroll_usd: float
    computed_at: datetime = field(default_factory=utc_now)

    @property
    def current_drawdown_pct(self) -> float:
        if self.peak_bankroll_usd <= 0:
            return 0.0
        return (self.peak_bankroll_usd - self.bankroll_usd) / self.peak_bankroll_usd * 100


# ─── ANALYTICS ────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CalibrationBucket:
    bucket_label: str       # e.g. "0.0-0.1"
    p_min: float
    p_max: float
    n_trades: int
    avg_p_model: float
    observed_win_rate: float
    calibration_error: float  # |avg_p - win_rate|


@dataclass(slots=True)
class PerformanceMetrics:
    """Comprehensive performance metrics."""
    n_trades: int
    n_wins: int
    n_losses: int
    hit_rate: float
    # PnL
    pnl_gross_usd: float
    pnl_net_usd: float
    avg_pnl_per_trade: float
    # EV
    ev_expected_usd: float     # sum(edge_net * size) at entry
    ev_realized_usd: float     # actual pnl
    ev_efficiency: float       # realized / expected
    # Probabilistic metrics
    brier_score: float         # lower is better (0=perfect)
    log_loss: float            # lower is better
    # Calibration
    calibration_buckets: list[CalibrationBucket]
    # Segmentation
    by_side: dict[str, dict]
    by_edge_bucket: dict[str, dict]   # e.g. "0.02-0.05": {trades, pnl, hit_rate}
    by_market: dict[str, dict]
    # Risk
    max_drawdown_usd: float
    sharpe_ratio: float | None
    profit_factor: float   # gross_profit / abs(gross_loss)
    avg_hold_hours: float
    computed_at: datetime = field(default_factory=utc_now)
