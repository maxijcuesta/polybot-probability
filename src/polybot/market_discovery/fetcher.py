"""
Market discovery: fetch active markets from Polymarket APIs.

Uses Gamma API for rich market metadata and CLOB API for orderbooks.
All errors are caught and logged — never crash the scan loop.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from ..models import MarketSnapshot, Orderbook, OrderbookLevel

logger = structlog.get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Timeouts
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0


def _parse_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO8601 / timestamp string to datetime."""
    if not value:
        return None
    # Try ISO format
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Try fromisoformat (Python 3.11+ handles Z)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    # Try unix timestamp
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _parse_json_string(value: Any) -> list:
    """Parse a JSON-encoded string like '["a","b"]' into a list. Returns [] on failure."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_gamma_market(raw: dict[str, Any]) -> MarketSnapshot | None:
    """
    Convert a Gamma API market dict to MarketSnapshot.

    Supports two API formats:
      Old: tokens=[{"token_id":...}], bestBid, bestAsk, openInterest
      New: clobTokenIds='["id1","id2"]', outcomePrices='["0.52","0.48"]', liquidityNum
    """
    try:
        # ── Token IDs ─────────────────────────────────────────────────────────
        # Old: raw["tokens"] = [{"token_id": "yes_id"}, {"token_id": "no_id"}]
        # New: raw["clobTokenIds"] = '["yes_id", "no_id"]'  (JSON-encoded string)
        tokens: list[dict] = raw.get("tokens", [])
        if len(tokens) >= 2:
            yes_token_id = str(tokens[0].get("token_id", ""))
            no_token_id  = str(tokens[1].get("token_id", ""))
        else:
            ids = _parse_json_string(raw.get("clobTokenIds", "[]"))
            if len(ids) < 2:
                logger.debug(
                    "fetcher.parse_skip_no_tokens",
                    market_id=raw.get("id", "?"),
                    has_tokens=bool(tokens),
                    has_clob_ids=bool(raw.get("clobTokenIds")),
                )
                return None
            yes_token_id = str(ids[0])
            no_token_id  = str(ids[1])

        if not yes_token_id or not no_token_id:
            return None

        market_id    = str(raw.get("id", raw.get("conditionId", "")))
        condition_id = str(raw.get("conditionId", raw.get("id", "")))

        # ── Prices ────────────────────────────────────────────────────────────
        # Old: bestBid / bestAsk at top level (floats)
        # New: outcomePrices = '["0.525", "0.475"]' — YES price / NO price
        #      outcomePrices[0] is a mid-price approximation for YES.
        #      The CLOB orderbook fetch will refine real bid/ask later.
        best_bid = _parse_float(raw.get("bestBid", raw.get("best_bid", 0.0)))
        best_ask = _parse_float(raw.get("bestAsk", raw.get("best_ask", 0.0)))

        if best_bid == 0.0 and best_ask == 0.0:
            prices = _parse_json_string(raw.get("outcomePrices", "[]"))
            if prices:
                yes_price = _parse_float(prices[0])
                # Use YES price as mid approximation; bid/ask spread added by sanity check
                best_bid = yes_price
                best_ask = yes_price  # triggers best_ask <= best_bid below → +0.01

        # Sanity bounds
        best_bid = max(0.0, min(1.0, best_bid))
        best_ask = max(0.0, min(1.0, best_ask))
        if best_ask <= best_bid:
            best_ask = min(1.0, best_bid + 0.01)

        # ── Open Interest ─────────────────────────────────────────────────────
        # Old: openInterest (float) at top level
        # New: absent at market level; liquidityNum (float) is the closest proxy
        open_interest = _parse_float(
            raw.get("openInterest")
            or raw.get("liquidityNum")
            or raw.get("liquidity")
            or 0.0
        )

        return MarketSnapshot(
            market_id=market_id,
            condition_id=condition_id,
            question=str(raw.get("question", "")),
            category=str(raw.get("category", raw.get("groupItemTitle", ""))),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            volume_24h=_parse_float(raw.get("volume24hr", raw.get("volumeClob", 0.0))),
            volume_total=_parse_float(raw.get("volume", 0.0)),
            open_interest=open_interest,
            last_trade_price=_parse_float(raw.get("lastTradePrice", 0.0)),
            last_trade_time=_parse_datetime(raw.get("lastTradeTime")),
            resolution_time=_parse_datetime(raw.get("endDate")),
            active=bool(raw.get("active", True)),
        )
    except Exception as exc:
        logger.warning(
            "fetcher.parse_error",
            market_id=raw.get("id", "unknown"),
            error=str(exc),
        )
        return None


def _parse_orderbook(raw: dict[str, Any]) -> Orderbook | None:
    """
    Parse CLOB /book response to Orderbook.

    CLOB book response:
        {
            "market": str,
            "asset_id": str,
            "bids": [{"price": str, "size": str}, ...],
            "asks": [{"price": str, "size": str}, ...],
        }
    """
    try:
        bids: list[OrderbookLevel] = []
        for level in raw.get("bids", []):
            price = _parse_float(level.get("price", 0))
            size = _parse_float(level.get("size", 0))
            if price > 0 and size > 0:
                bids.append(OrderbookLevel(price=price, size=size))

        asks: list[OrderbookLevel] = []
        for level in raw.get("asks", []):
            price = _parse_float(level.get("price", 0))
            size = _parse_float(level.get("size", 0))
            if price > 0 and size > 0:
                asks.append(OrderbookLevel(price=price, size=size))

        # Sort: bids descending (best bid first), asks ascending (best ask first)
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return Orderbook(bids=bids, asks=asks)
    except Exception as exc:
        logger.warning("fetcher.orderbook_parse_error", error=str(exc))
        return None


class MarketFetcher:
    """
    Fetches market data from Polymarket APIs.

    Uses Gamma API for market metadata (rich, includes volume/OI/category)
    and CLOB API for orderbooks.

    Thread-safe: creates its own httpx.AsyncClient session.
    """

    def __init__(
        self,
        clob_base: str = CLOB_BASE,
        gamma_base: str = GAMMA_BASE,
    ) -> None:
        self._clob_base = clob_base.rstrip("/")
        self._gamma_base = gamma_base.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=_READ_TIMEOUT,
            write=_READ_TIMEOUT,
            pool=_READ_TIMEOUT,
        )
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "MarketFetcher":
        await self._get_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ─── PUBLIC API ───────────────────────────────────────────────────────────

    async def fetch_active_markets(self, limit: int = 100) -> list[MarketSnapshot]:
        """
        Fetch active binary markets from Gamma API.

        Paginates automatically up to `limit` total markets.
        Skips markets that fail validation (non-binary, parse errors, etc.).

        Returns list of MarketSnapshot objects sorted by volume_24h desc.
        """
        client = await self._get_client()
        markets: list[MarketSnapshot] = []
        offset = 0
        page_size = min(limit, 100)

        while len(markets) < limit:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
            }
            url = f"{self._gamma_base}/markets"
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "fetcher.gamma_http_error",
                    url=url,
                    status=exc.response.status_code,
                    offset=offset,
                )
                break
            except httpx.RequestError as exc:
                logger.error("fetcher.gamma_request_error", url=url, error=str(exc), offset=offset)
                break
            except Exception as exc:
                logger.error("fetcher.gamma_unexpected", url=url, error=str(exc), offset=offset)
                break

            # Gamma API returns either a plain list or a paginated dict.
            # Old format: [{...}, ...]
            # New format: {"data": [{...}, ...], "count": N, "next_cursor": "..."}
            if isinstance(data, list):
                raw_list: list[dict] = data
            elif isinstance(data, dict):
                raw_list = data.get("data", data.get("results", data.get("markets", [])))
                if not isinstance(raw_list, list):
                    logger.warning(
                        "fetcher.unexpected_response_shape",
                        url=url,
                        offset=offset,
                        keys=list(data.keys()),
                        sample=str(data)[:300],
                    )
                    raw_list = []
            else:
                logger.warning(
                    "fetcher.unexpected_response_type",
                    url=url,
                    offset=offset,
                    type=type(data).__name__,
                    sample=str(data)[:300],
                )
                raw_list = []

            logger.debug(
                "fetcher.page_received",
                url=url,
                offset=offset,
                raw_count=len(raw_list),
            )

            if not raw_list:
                break  # No more pages

            parsed_before = len(markets)
            for raw in raw_list:
                snapshot = _parse_gamma_market(raw)
                if snapshot is not None:
                    markets.append(snapshot)

            logger.debug(
                "fetcher.page_parsed",
                offset=offset,
                raw=len(raw_list),
                parsed=len(markets) - parsed_before,
                running_total=len(markets),
            )

            if len(raw_list) < page_size:
                break  # Last page

            offset += page_size

        logger.info(
            "fetcher.markets_fetched",
            total=len(markets),
            limit=limit,
        )

        # Sort by 24h volume descending (most liquid first)
        markets.sort(key=lambda m: m.volume_24h, reverse=True)
        return markets[:limit]

    async def fetch_orderbook(self, token_id: str) -> Orderbook | None:
        """
        Fetch orderbook for a specific token from CLOB API.

        GET /book?token_id={token_id}
        Returns Orderbook or None on error.
        """
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self._clob_base}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            raw = resp.json()
            return _parse_orderbook(raw)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "fetcher.orderbook_http_error",
                token_id=token_id,
                status=exc.response.status_code,
            )
        except httpx.RequestError as exc:
            logger.warning("fetcher.orderbook_request_error", token_id=token_id, error=str(exc))
        except Exception as exc:
            logger.warning("fetcher.orderbook_unexpected", token_id=token_id, error=str(exc))
        return None

    async def fetch_market_trades(
        self,
        market_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent trades for a market from CLOB API.

        GET /trades?market={market_id}&limit={limit}
        Returns raw trade dicts (price, size, side, timestamp).
        """
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self._clob_base}/trades",
                params={"market": market_id, "limit": limit},
            )
            resp.raise_for_status()
            result = resp.json()
            return result if isinstance(result, list) else []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "fetcher.trades_http_error",
                market_id=market_id,
                status=exc.response.status_code,
            )
        except httpx.RequestError as exc:
            logger.warning("fetcher.trades_request_error", market_id=market_id, error=str(exc))
        except Exception as exc:
            logger.warning("fetcher.trades_unexpected", market_id=market_id, error=str(exc))
        return []

    async def enrich_with_orderbooks(
        self,
        markets: list[MarketSnapshot],
        token_side: str = "yes",  # "yes" or "no"
    ) -> list[MarketSnapshot]:
        """
        Fetch and attach orderbooks to a list of MarketSnapshots.

        Fetches the YES token orderbook by default (most representative).
        Modifies snapshots in-place (sets orderbook field) and also updates
        best_bid/best_ask from the live orderbook if available.

        Returns the same list with orderbooks populated where fetch succeeded.
        """
        for market in markets:
            token_id = market.yes_token_id if token_side == "yes" else market.no_token_id
            ob = await self.fetch_orderbook(token_id)
            if ob is not None:
                # Use object.__setattr__ since MarketSnapshot uses slots
                object.__setattr__(market, "orderbook", ob)
                if ob.bids and ob.asks:
                    object.__setattr__(market, "best_bid", ob.best_bid)
                    object.__setattr__(market, "best_ask", ob.best_ask)
        return markets
