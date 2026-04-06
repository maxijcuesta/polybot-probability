"""
Modelos de datos para responses del CLOB API.

Pydantic v2 con validación estricta.
Usados para deserializar las respuestas REST y WebSocket.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    DELAYED = "DELAYED"
    UNMATCHED = "UNMATCHED"
    CANCELED = "CANCELED"
    FILLED = "FILLED"
    MTC = "MTC"  # Matched, then cancelled


class OrderType(str, Enum):
    GTC = "GTC"   # Good Till Cancelled
    FOK = "FOK"   # Fill or Kill
    GTD = "GTD"   # Good Till Date


class TokenOutcome(BaseModel):
    """Un outcome dentro de un mercado (token tradeable)."""
    token_id: str
    outcome: str  # "Yes", "No", etc.
    price: float = 0.0
    winner: bool = False


class Market(BaseModel):
    """Mercado del CLOB con todos sus metadatos."""
    condition_id: str
    question_id: str = ""
    question: str = ""
    description: str = ""
    market_slug: str = ""
    tokens: list[TokenOutcome] = Field(default_factory=list)
    rewards: dict[str, Any] = Field(default_factory=dict)
    minimum_order_size: float = 1.0
    minimum_tick_size: float = 0.01
    category: str = ""
    end_date_iso: str = ""
    game_start_time: str = ""
    seconds_delay: int = 0
    fpmm: str = ""
    active: bool = True
    closed: bool = False
    archived: bool = False
    accepting_orders: bool = True
    accepting_order_timestamp: str = ""
    notifications_enabled: bool = True
    neg_risk: bool = False
    neg_risk_market_id: str = ""
    neg_risk_request_id: str = ""
    icon: str = ""
    image: str = ""
    tags: list[str] = Field(default_factory=list)
    volume: float = 0.0
    volume_24hr: float = 0.0
    liquidity: float = 0.0

    @property
    def end_date(self) -> datetime | None:
        if not self.end_date_iso:
            return None
        try:
            return datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def hours_to_resolution(self) -> float | None:
        end = self.end_date
        if not end:
            return None
        delta = end - datetime.now(tz=end.tzinfo)
        return delta.total_seconds() / 3600

    @property
    def yes_token_id(self) -> str | None:
        for t in self.tokens:
            if t.outcome.lower() in ("yes", "y"):
                return t.token_id
        return self.tokens[0].token_id if self.tokens else None

    @property
    def no_token_id(self) -> str | None:
        for t in self.tokens:
            if t.outcome.lower() in ("no", "n"):
                return t.token_id
        return self.tokens[1].token_id if len(self.tokens) > 1 else None


class OrderbookLevel(BaseModel):
    """Un nivel del orderbook (bid o ask)."""
    price: float
    size: float

    @field_validator("price", "size", mode="before")
    @classmethod
    def parse_decimal_string(cls, v: Any) -> float:
        return float(v)


class Orderbook(BaseModel):
    """Snapshot del orderbook de un token."""
    market: str = ""
    asset_id: str = ""
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)
    hash: str = ""
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float | None:
        if not self.bids:
            return None
        return max(b.price for b in self.bids)

    @property
    def best_ask(self) -> float | None:
        if not self.asks:
            return None
        return min(a.price for a in self.asks)

    @property
    def midpoint(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb

    @property
    def spread_pct(self) -> float | None:
        mid = self.midpoint
        spread = self.spread
        if mid is None or spread is None or mid == 0:
            return None
        return (spread / mid) * 100

    def bid_liquidity(self, depth: float = 0.05) -> float:
        """Liquidez en bids dentro de `depth` del best bid."""
        bb = self.best_bid
        if bb is None:
            return 0.0
        return sum(
            level.size
            for level in self.bids
            if level.price >= bb * (1 - depth)
        )

    def ask_liquidity(self, depth: float = 0.05) -> float:
        """Liquidez en asks dentro de `depth` del best ask."""
        ba = self.best_ask
        if ba is None:
            return 0.0
        return sum(
            level.size
            for level in self.asks
            if level.price <= ba * (1 + depth)
        )


class Order(BaseModel):
    """Orden enviada al CLOB."""
    order_id: str = ""
    status: OrderStatus = OrderStatus.LIVE
    owner: str = ""
    maker: str = ""
    market: str = ""
    asset_id: str = ""
    side: Side = Side.BUY
    original_size: float = 0.0
    remaining_size: float = 0.0
    price: float = 0.0
    type: OrderType = OrderType.GTC
    expiration: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def filled_size(self) -> float:
        return self.original_size - self.remaining_size

    @property
    def fill_pct(self) -> float:
        if self.original_size == 0:
            return 0.0
        return (self.filled_size / self.original_size) * 100


class Fill(BaseModel):
    """Fill de una orden (ejecución parcial o total)."""
    trade_id: str = ""
    order_id: str = ""
    market: str = ""
    asset_id: str = ""
    side: Side = Side.BUY
    price: float = 0.0
    size: float = 0.0
    fee_rate_bps: float = 0.0
    created_at: float = 0.0
    maker_address: str = ""
    taker_address: str = ""

    @property
    def fee_usdc(self) -> float:
        return self.size * (self.fee_rate_bps / 10000)

    @property
    def net_cost(self) -> float:
        return self.size + self.fee_usdc


class Position(BaseModel):
    """Posición abierta en un mercado."""
    asset_id: str
    condition_id: str = ""
    outcome: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    initial_value: float = 0.0

    @property
    def current_value(self) -> float:
        return self.size * self.current_price

    @property
    def return_pct(self) -> float:
        if self.initial_value == 0:
            return 0.0
        return ((self.current_value - self.initial_value) / self.initial_value) * 100


class PriceHistory(BaseModel):
    """Punto de historial de precio."""
    t: int  # timestamp unix
    p: float  # precio

    @field_validator("p", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> float:
        return float(v)
