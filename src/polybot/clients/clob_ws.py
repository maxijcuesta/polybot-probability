"""
Cliente WebSocket para Polymarket CLOB.

El WebSocket es la FUENTE PRINCIPAL de datos de precio y orderbook.
REST se usa solo para operaciones puntuales (crear orden, consultar posición).

Canales disponibles:
  - market: updates de precio/orderbook para mercados específicos
  - user: fills, órdenes, notificaciones de la wallet

Endpoint:
  wss://ws-subscriptions-clob.polymarket.com/ws/market
  wss://ws-subscriptions-clob.polymarket.com/ws/user

Reconexión automática con backoff exponencial.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .auth import PolymarketAuth

logger = structlog.get_logger(__name__)

WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws"


def _make_ssl_context() -> ssl.SSLContext:
    """
    Crea un SSLContext que funciona en macOS y Linux/Docker.

    macOS no pasa los certificados del sistema a Python automáticamente.
    Se usa certifi si está disponible; si no, el contexto por defecto.
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except ImportError:
        pass
    return ssl.create_default_context()
PING_INTERVAL = 20  # segundos
PING_TIMEOUT = 10   # segundos
MAX_RECONNECT_ATTEMPTS = 0  # 0 = infinito
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
BACKOFF_MULTIPLIER = 2.0


class WebSocketMessage:
    """Mensaje recibido del WebSocket parseado."""

    __slots__ = ("event_type", "data", "asset_id", "timestamp", "raw")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.event_type: str = raw.get("event_type", raw.get("type", "unknown"))
        self.asset_id: str = raw.get("asset_id", raw.get("market", ""))
        self.timestamp: float = raw.get("timestamp", time.time())
        self.data = raw

    @property
    def is_price_change(self) -> bool:
        return self.event_type == "price_change"

    @property
    def is_book_update(self) -> bool:
        return self.event_type == "book"

    @property
    def is_tick_size_change(self) -> bool:
        return self.event_type == "tick_size_change"

    @property
    def is_last_trade_price(self) -> bool:
        return self.event_type == "last_trade_price"

    def __repr__(self) -> str:
        return f"WSMsg(type={self.event_type}, asset={self.asset_id})"


class ClobWebSocketClient:
    """
    Cliente WebSocket para Polymarket CLOB.

    Características:
    - Reconexión automática con backoff exponencial
    - Resubscripción automática tras reconexión
    - Ping/pong para mantener conexión viva
    - Callback system para handlers por tipo de mensaje
    - Modo dry_run: genera mensajes simulados para testing

    Uso típico:
        async with ClobWebSocketClient(auth) as ws:
            await ws.subscribe_markets(["token_id_1", "token_id_2"])
            async for msg in ws.messages():
                print(msg.event_type, msg.data)
    """

    def __init__(
        self,
        auth: PolymarketAuth,
        dry_run: bool = True,
        channel: str = "market",  # "market" o "user"
    ) -> None:
        self.auth = auth
        self.dry_run = dry_run
        self.channel = channel
        self._ws_url = f"{WS_BASE}/{channel}"

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subscribed_assets: set[str] = set()
        self._message_queue: asyncio.Queue[WebSocketMessage | None] = asyncio.Queue()
        self._handlers: dict[str, list[Callable]] = {}
        self._running = False
        self._reconnect_task: asyncio.Task | None = None
        self._stats = {
            "messages_received": 0,
            "reconnections": 0,
            "last_message_at": 0.0,
            "connected_at": 0.0,
        }

    async def __aenter__(self) -> "ClobWebSocketClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # ─── CONEXIÓN ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Inicia la conexión WebSocket y el loop de reconexión."""
        self._running = True
        if self.dry_run:
            logger.info("ws.dry_run_mode", message="WebSocket en modo simulación")
            asyncio.create_task(self._simulate_messages())
            return

        self._reconnect_task = asyncio.create_task(self._connection_loop())

    async def disconnect(self) -> None:
        """Cierra la conexión limpiamente."""
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()

        # Señal de fin de stream
        await self._message_queue.put(None)
        logger.info("ws.disconnected")

    async def _connection_loop(self) -> None:
        """Loop principal de conexión con reconexión automática."""
        backoff = INITIAL_BACKOFF
        attempt = 0

        while self._running:
            try:
                logger.info(
                    "ws.connecting",
                    url=self._ws_url,
                    attempt=attempt,
                    subscriptions=len(self._subscribed_assets),
                )

                async with websockets.connect(
                    self._ws_url,
                    ssl=_make_ssl_context(),
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                    close_timeout=10,
                    max_size=2 ** 23,  # 8MB máx para mensajes grandes
                ) as ws:
                    self._ws = ws
                    self._stats["connected_at"] = time.time()
                    backoff = INITIAL_BACKOFF  # reset tras conexión exitosa
                    attempt = 0

                    logger.info("ws.connected", url=self._ws_url)

                    # Resubscribir a todos los assets previos
                    if self._subscribed_assets:
                        await self._send_subscription(list(self._subscribed_assets))

                    # Loop de recepción de mensajes
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        await self._handle_raw_message(raw_msg)

            except ConnectionClosed as e:
                logger.warning(
                    "ws.connection_closed",
                    code=e.code,
                    reason=e.reason,
                    attempt=attempt,
                )
            except WebSocketException as e:
                logger.warning("ws.error", error=str(e), attempt=attempt)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ws.unexpected_error", error=str(e), attempt=attempt)

            if not self._running:
                break

            self._ws = None
            self._stats["reconnections"] += 1
            attempt += 1

            logger.info("ws.reconnecting", backoff=backoff, attempt=attempt)
            await asyncio.sleep(backoff)
            backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)

    # ─── SUBSCRIPCIÓN ─────────────────────────────────────────────────────────

    async def subscribe_markets(self, asset_ids: list[str]) -> None:
        """
        Subscribe a updates de precio/orderbook de los tokens dados.

        Args:
            asset_ids: lista de token IDs (cada outcome tiene su token_id)
        """
        new_assets = [a for a in asset_ids if a not in self._subscribed_assets]
        if not new_assets:
            return

        self._subscribed_assets.update(new_assets)
        logger.info("ws.subscribing", assets=new_assets, total=len(self._subscribed_assets))

        if self.dry_run:
            return

        if self._ws and not self._ws.closed:
            await self._send_subscription(new_assets)

    async def unsubscribe_markets(self, asset_ids: list[str]) -> None:
        """Cancela subscripción a tokens específicos."""
        for asset_id in asset_ids:
            self._subscribed_assets.discard(asset_id)

        if self.dry_run or not self._ws or self._ws.closed:
            return

        msg = {
            "type": "unsubscribe",
            "channel": self.channel,
            "assets_id": asset_ids,
        }
        await self._ws.send(json.dumps(msg))
        logger.info("ws.unsubscribed", assets=asset_ids)

    async def _send_subscription(self, asset_ids: list[str]) -> None:
        """Envía mensaje de subscripción al WebSocket."""
        if not self._ws or self._ws.closed:
            return

        # Para canal market: subscripción por asset IDs
        msg: dict[str, Any] = {
            "type": "subscribe",
            "channel": self.channel,
            "assets_id": asset_ids,
        }

        # Para canal user: incluir autenticación
        if self.channel == "user":
            auth_headers = self.auth.get_api_key_headers()
            msg["auth"] = {
                "apiKey": auth_headers.get("POLY_API_KEY", ""),
                "secret": auth_headers.get("POLY_SECRET", ""),
                "passphrase": auth_headers.get("POLY_PASSPHRASE", ""),
            }

        await self._ws.send(json.dumps(msg))

    # ─── PROCESAMIENTO DE MENSAJES ────────────────────────────────────────────

    async def _handle_raw_message(self, raw: str | bytes) -> None:
        """Parsea y encola un mensaje recibido."""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            data = json.loads(raw)

            # El CLOB puede enviar una lista de updates o un objeto
            messages = data if isinstance(data, list) else [data]

            for msg_data in messages:
                msg = WebSocketMessage(msg_data)
                self._stats["messages_received"] += 1
                self._stats["last_message_at"] = time.time()

                # Encolar para consumo por generators
                await self._message_queue.put(msg)

                # Ejecutar handlers registrados
                await self._dispatch(msg)

        except json.JSONDecodeError as e:
            logger.warning("ws.parse_error", error=str(e), raw=str(raw)[:100])
        except Exception as e:
            logger.error("ws.handler_error", error=str(e))

    async def _dispatch(self, msg: WebSocketMessage) -> None:
        """Ejecuta callbacks registrados para el tipo de mensaje."""
        handlers = self._handlers.get(msg.event_type, []) + self._handlers.get("*", [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(msg)
                else:
                    handler(msg)
            except Exception as e:
                logger.error(
                    "ws.handler_exception",
                    handler=handler.__name__,
                    error=str(e),
                )

    # ─── API PÚBLICA ──────────────────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable) -> None:
        """
        Registra un handler para un tipo de mensaje.

        event_type: "price_change", "book", "last_trade_price", "*" (todos)
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def messages(self) -> AsyncGenerator[WebSocketMessage, None]:
        """
        Generator async de mensajes.

        Uso:
            async for msg in ws.messages():
                process(msg)
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=30.0,
                )
                if msg is None:
                    break
                yield msg
            except asyncio.TimeoutError:
                # No hay mensajes en 30s — revisar si sigue conectado
                if not self._running:
                    break
                continue

    @property
    def is_connected(self) -> bool:
        """True si el WebSocket está actualmente conectado."""
        if self.dry_run:
            return self._running
        return self._ws is not None and not self._ws.closed

    @property
    def stats(self) -> dict[str, Any]:
        """Estadísticas de la conexión."""
        return {
            **self._stats,
            "connected": self.is_connected,
            "subscriptions": len(self._subscribed_assets),
            "queue_size": self._message_queue.qsize(),
        }

    # ─── MODO DRY RUN ─────────────────────────────────────────────────────────

    async def _simulate_messages(self) -> None:
        """
        Genera mensajes simulados para testing sin WebSocket real.

        Simula updates de precio aleatorios para los assets subscriptos.
        """
        import random

        logger.info("ws.simulator_started")
        self._stats["connected_at"] = time.time()

        while self._running:
            await asyncio.sleep(2.0)  # simula llegada de mensajes cada 2s

            if not self._subscribed_assets:
                continue

            asset_id = random.choice(list(self._subscribed_assets))
            base_price = random.uniform(0.3, 0.7)

            # Simula price_change
            price_msg = WebSocketMessage({
                "event_type": "price_change",
                "asset_id": asset_id,
                "market": asset_id,
                "price": f"{base_price:.4f}",
                "side": random.choice(["BUY", "SELL"]),
                "size": f"{random.uniform(10, 500):.2f}",
                "timestamp": time.time(),
            })

            self._stats["messages_received"] += 1
            self._stats["last_message_at"] = time.time()
            await self._message_queue.put(price_msg)
            await self._dispatch(price_msg)

            # Simula book update ocasionalmente
            if random.random() < 0.3:
                bids = [
                    {"price": f"{base_price - i * 0.01:.4f}", "size": f"{random.uniform(100, 1000):.2f}"}
                    for i in range(1, 6)
                ]
                asks = [
                    {"price": f"{base_price + i * 0.01:.4f}", "size": f"{random.uniform(100, 1000):.2f}"}
                    for i in range(1, 6)
                ]

                book_msg = WebSocketMessage({
                    "event_type": "book",
                    "asset_id": asset_id,
                    "market": asset_id,
                    "bids": bids,
                    "asks": asks,
                    "timestamp": time.time(),
                })
                await self._message_queue.put(book_msg)
                await self._dispatch(book_msg)

        logger.info("ws.simulator_stopped")
