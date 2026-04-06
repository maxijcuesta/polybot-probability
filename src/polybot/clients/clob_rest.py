"""
Cliente REST para Polymarket CLOB API.

Endpoints base:
  Mainnet: https://clob.polymarket.com
  Gamma (markets metadata): https://gamma-api.polymarket.com

Rate limiting: máx ~10 req/seg con backoff exponencial.
Todos los endpoints de escritura requieren auth L1 + L2.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import structlog

from .auth import PolymarketAuth
from .rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Timeouts
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 5
INITIAL_BACKOFF = 0.5  # segundos
MAX_BACKOFF = 60.0


class ClobRestClient:
    """
    Cliente REST async para Polymarket CLOB.

    Características:
    - Rate limiter integrado (~10 req/seg)
    - Backoff exponencial con jitter
    - Retry automático en errores transitorios (5xx, timeout, rate limit)
    - Modo dry_run para testear sin wallet
    - Logging estructurado de todas las requests
    """

    def __init__(self, auth: PolymarketAuth, dry_run: bool = True) -> None:
        self.auth = auth
        self.dry_run = dry_run
        self.rate_limiter = RateLimiter(max_per_second=8)  # conservador bajo el límite

        self._client: httpx.AsyncClient | None = None
        self._gamma_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ClobRestClient":
        await self._init_clients()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _init_clients(self) -> None:
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT,
            read=READ_TIMEOUT,
            write=READ_TIMEOUT,
            pool=READ_TIMEOUT,
        )
        self._client = httpx.AsyncClient(
            base_url=CLOB_BASE,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        self._gamma_client = httpx.AsyncClient(
            base_url=GAMMA_BASE,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        if self._gamma_client:
            await self._gamma_client.aclose()

    # ─── REQUEST ENGINE ────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        auth_level: int = 0,  # 0=none, 1=api_key, 2=api_key+l2
        gamma: bool = False,
    ) -> dict[str, Any] | list[Any]:
        """
        Ejecuta un request con retry y backoff exponencial.

        auth_level:
          0 — sin autenticación (endpoints públicos)
          1 — API Key headers
          2 — API Key + firma EIP-712
        """
        if self._client is None:
            await self._init_clients()

        client = self._gamma_client if gamma else self._client
        headers: dict[str, str] = {}

        if auth_level >= 1:
            headers.update(self.auth.get_api_key_headers())
        if auth_level >= 2:
            headers.update(self.auth.sign_l2_auth())

        body_str = json.dumps(json_body) if json_body else ""
        backoff = INITIAL_BACKOFF

        for attempt in range(MAX_RETRIES):
            await self.rate_limiter.acquire()

            try:
                response = await client.request(
                    method=method,
                    url=path,
                    params=params,
                    json=json_body,
                    headers=headers,
                )

                logger.debug(
                    "rest.request",
                    method=method,
                    path=path,
                    status=response.status_code,
                    attempt=attempt,
                )

                # Rate limit — esperar y reintentar
                if response.status_code == 429:
                    wait = float(response.headers.get("Retry-After", backoff))
                    logger.warning("rest.rate_limited", wait=wait, attempt=attempt)
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

                # Errores de servidor — reintentar
                if response.status_code >= 500:
                    logger.warning(
                        "rest.server_error",
                        status=response.status_code,
                        attempt=attempt,
                        body=response.text[:200],
                    )
                    await asyncio.sleep(backoff + (attempt * 0.1))
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

                # Error de cliente — no reintentar
                if response.status_code >= 400:
                    logger.error(
                        "rest.client_error",
                        status=response.status_code,
                        path=path,
                        body=response.text[:500],
                    )
                    response.raise_for_status()

                return response.json()

            except httpx.TimeoutException:
                logger.warning("rest.timeout", path=path, attempt=attempt)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except httpx.NetworkError as e:
                logger.warning("rest.network_error", error=str(e), attempt=attempt)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        raise RuntimeError(
            f"Request fallido tras {MAX_RETRIES} intentos: {method} {path}"
        )

    # ─── ENDPOINTS PÚBLICOS ────────────────────────────────────────────────────

    async def get_markets(
        self,
        next_cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Lista mercados activos del CLOB.
        Devuelve cursor para paginación.
        """
        params: dict[str, Any] = {"limit": limit}
        if next_cursor:
            params["next_cursor"] = next_cursor

        return await self._request("GET", "/markets", params=params)  # type: ignore[return-value]

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Detalle de un mercado específico."""
        return await self._request("GET", f"/markets/{condition_id}")  # type: ignore[return-value]

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """
        Orderbook actual de un token.

        Returns:
            {
                "market": str,
                "asset_id": str,
                "bids": [{"price": str, "size": str}, ...],
                "asks": [{"price": str, "size": str}, ...],
                "hash": str,
            }
        """
        return await self._request("GET", "/book", params={"token_id": token_id})  # type: ignore[return-value]

    async def get_midpoint(self, token_id: str) -> dict[str, Any]:
        """Precio midpoint de un token."""
        return await self._request("GET", "/midpoint", params={"token_id": token_id})  # type: ignore[return-value]

    async def get_spread(self, token_id: str) -> dict[str, Any]:
        """Spread actual de un token."""
        return await self._request("GET", "/spread", params={"token_id": token_id})  # type: ignore[return-value]

    async def get_price(self, token_id: str, side: str) -> dict[str, Any]:
        """
        Precio de mercado para un lado (BUY o SELL).

        Args:
            side: "BUY" o "SELL"
        """
        return await self._request(  # type: ignore[return-value]
            "GET",
            "/price",
            params={"token_id": token_id, "side": side},
        )

    async def get_prices_history(
        self,
        market: str,
        fidelity: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Historial de precios de un mercado.

        Args:
            fidelity: granularidad en minutos (1, 5, 60, etc.)
        """
        params: dict[str, Any] = {"market": market, "fidelity": fidelity}
        if start_ts:
            params["startTs"] = start_ts
        if end_ts:
            params["endTs"] = end_ts

        result = await self._request("GET", "/prices-history", params=params)
        return result if isinstance(result, list) else result.get("history", [])  # type: ignore[union-attr]

    async def get_trades(
        self,
        market: str | None = None,
        maker_address: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Historial de trades de un mercado o wallet."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if maker_address:
            params["maker_address"] = maker_address

        result = await self._request("GET", "/trades", params=params)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_last_trade_price(self, token_id: str) -> dict[str, Any]:
        """Último precio negociado de un token."""
        return await self._request("GET", "/last-trade-price", params={"token_id": token_id})  # type: ignore[return-value]

    # ─── GAMMA API (metadata de mercados) ─────────────────────────────────────

    async def get_gamma_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        liquidity_min: float | None = None,
        volume_min: float | None = None,
        tag_slugs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Busca mercados con filtros en Gamma API.

        Args:
            end_date_min: ISO date string (ej: "2024-01-01")
            liquidity_min: liquidez mínima en USDC
            tag_slugs: categorías (ej: ["politics", "crypto"])
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        if end_date_min:
            params["end_date_min"] = end_date_min
        if end_date_max:
            params["end_date_max"] = end_date_max
        if liquidity_min is not None:
            params["liquidity_min"] = liquidity_min
        if volume_min is not None:
            params["volume_min"] = volume_min
        if tag_slugs:
            params["tag_slug"] = ",".join(tag_slugs)

        result = await self._request("GET", "/markets", params=params, gamma=True)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_gamma_market(self, condition_id: str) -> dict[str, Any]:
        """Detalle de un mercado desde Gamma API (incluye metadata enriquecida)."""
        result = await self._request("GET", f"/markets/{condition_id}", gamma=True)
        return result  # type: ignore[return-value]

    async def get_gamma_events(
        self,
        limit: int = 50,
        active: bool = True,
        tag_slugs: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Lista eventos (grupos de mercados relacionados)."""
        params: dict[str, Any] = {"limit": limit, "active": str(active).lower()}
        if tag_slugs:
            params["tag_slug"] = ",".join(tag_slugs)

        result = await self._request("GET", "/events", params=params, gamma=True)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    # ─── ENDPOINTS AUTENTICADOS (ÓRDENES) ─────────────────────────────────────

    async def get_api_keys(self) -> list[dict[str, Any]]:
        """Lista las API keys de la wallet. Requiere auth L2."""
        result = await self._request("GET", "/auth/api-key", auth_level=2)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def create_api_key(self) -> dict[str, Any]:
        """Crea una nueva API key. Requiere auth L2."""
        return await self._request("POST", "/auth/api-key", auth_level=2)  # type: ignore[return-value]

    async def get_open_orders(
        self,
        market: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Órdenes abiertas de la wallet. Requiere auth L1."""
        params: dict[str, Any] = {}
        if market:
            params["market"] = market
        if asset_id:
            params["asset_id"] = asset_id

        result = await self._request("GET", "/orders", params=params, auth_level=1)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Estado de una orden específica. Requiere auth L1."""
        return await self._request("GET", f"/orders/{order_id}", auth_level=1)  # type: ignore[return-value]

    async def create_order(
        self,
        token_id: str,
        side: str,  # "BUY" o "SELL"
        price: float,
        size: float,
        order_type: str = "GTC",  # GTC, FOK, GTD
        expiration: int = 0,  # timestamp unix, 0 = sin expiración
    ) -> dict[str, Any]:
        """
        Crea una orden limit en el CLOB.

        En dry_run: simula la orden sin enviarla.
        En live: firma con EIP-712 y envía al CLOB.

        Args:
            token_id: ID del token (outcome) a operar
            side: "BUY" o "SELL"
            price: precio en USDC (0.01 - 0.99)
            size: tamaño en USDC
            order_type: GTC (default), FOK, GTD
            expiration: unix timestamp de expiración (solo GTD)

        Returns:
            {
                "orderID": str,
                "status": "matched" | "unmatched" | "delayed",
                ...
            }
        """
        if self.dry_run:
            order_id = f"dry-run-{int(time.time() * 1000)}"
            logger.info(
                "rest.dry_run_order",
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )
            return {
                "orderID": order_id,
                "status": "dry_run",
                "token_id": token_id,
                "side": side,
                "price": str(price),
                "size": str(size),
                "type": order_type,
            }

        import random

        # Construir datos de la orden
        salt = random.randint(1, 2**256 - 1)
        maker_amount = int(size * 1_000_000)  # USDC tiene 6 decimales
        taker_amount = int(size / price * 1_000_000) if side == "BUY" else int(size * price * 1_000_000)

        order_data = {
            "salt": salt,
            "maker": self.auth.wallet_address,
            "signer": self.auth.wallet_address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": 0,
            "feeRateBps": 0,
            "side": 0 if side == "BUY" else 1,
            "signatureType": 0,  # EOA
        }

        signature = self.auth.sign_order(order_data)

        payload = {
            "order": {**order_data, "signature": signature},
            "owner": self.auth.wallet_address,
            "orderType": order_type,
        }

        return await self._request(  # type: ignore[return-value]
            "POST",
            "/order",
            json_body=payload,
            auth_level=2,
        )

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """
        Cancela una orden por ID. Requiere auth L2.

        En dry_run: simula la cancelación.
        """
        if self.dry_run:
            logger.info("rest.dry_run_cancel", order_id=order_id)
            return {"canceled": [order_id]}

        return await self._request(  # type: ignore[return-value]
            "DELETE",
            f"/order/{order_id}",
            auth_level=2,
        )

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        """Cancela múltiples órdenes en batch. Requiere auth L2."""
        if self.dry_run:
            logger.info("rest.dry_run_cancel_batch", order_ids=order_ids)
            return {"canceled": order_ids}

        return await self._request(  # type: ignore[return-value]
            "DELETE",
            "/orders",
            json_body=order_ids,
            auth_level=2,
        )

    async def cancel_all_orders(self) -> dict[str, Any]:
        """Cancela TODAS las órdenes abiertas. Requiere auth L2."""
        if self.dry_run:
            logger.info("rest.dry_run_cancel_all")
            return {"canceled": "all"}

        return await self._request("DELETE", "/orders", auth_level=2)  # type: ignore[return-value]

    async def cancel_market_orders(
        self,
        market: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancela todas las órdenes de un mercado. Requiere auth L2."""
        if self.dry_run:
            logger.info("rest.dry_run_cancel_market", market=market)
            return {"canceled": "market"}

        payload: dict[str, Any] = {"market": market}
        if asset_id:
            payload["asset_id"] = asset_id

        return await self._request(  # type: ignore[return-value]
            "DELETE",
            "/orders/market",
            json_body=payload,
            auth_level=2,
        )

    # ─── POSICIONES ───────────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        Posiciones abiertas de la wallet.

        Requiere auth L1. Devuelve balance de tokens por mercado.
        """
        result = await self._request("GET", "/positions", auth_level=1)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_balance_allowance(self, asset_type: str = "USDC") -> dict[str, Any]:
        """Balance y allowance de USDC en el contrato CTF."""
        return await self._request(  # type: ignore[return-value]
            "GET",
            "/balance-allowance",
            params={"asset_type": asset_type},
            auth_level=1,
        )

    # ─── NOTIFICACIONES Y NEGOCIACIÓN ─────────────────────────────────────────

    async def get_notifications(self) -> list[dict[str, Any]]:
        """Notificaciones de fills, cancelaciones, etc. Requiere auth L1."""
        result = await self._request("GET", "/notifications", auth_level=1)
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_tick_size(self, token_id: str) -> dict[str, Any]:
        """Tick size mínimo para un token."""
        return await self._request("GET", "/tick-size", params={"token_id": token_id})  # type: ignore[return-value]

    async def get_neg_risk(self, token_id: str) -> dict[str, Any]:
        """Info de neg-risk para mercados multi-outcome."""
        return await self._request("GET", "/neg-risk", params={"token_id": token_id})  # type: ignore[return-value]

    # ─── HEALTH Y ESTADO ──────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """
        Verifica que el CLOB esté operativo.

        GET / devuelve la string "OK" (no un dict), así que verificamos
        que la respuesta no sea None (sin lanzar excepción = OK).
        """
        try:
            result = await self._request("GET", "/")
            return result is not None
        except Exception as e:
            logger.warning("rest.health_check_failed", error=str(e))
            return False

    async def get_sampling_markets(self) -> list[dict[str, Any]]:
        """Mercados de sampling activos (subset de alta liquidez)."""
        result = await self._request("GET", "/sampling-markets")
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def get_sampling_simplified_markets(self) -> list[dict[str, Any]]:
        """Versión simplificada de sampling markets."""
        result = await self._request("GET", "/sampling-simplified-markets")
        return result if isinstance(result, list) else []  # type: ignore[return-value]
