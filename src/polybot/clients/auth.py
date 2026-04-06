"""
Polymarket authentication via EIP-712 signing.

Polymarket CLOB usa dos niveles de autenticación:
1. API Key (L1) — para endpoints de solo lectura
2. Wallet signing EIP-712 (L2) — para órdenes y posiciones

La private key NUNCA se hardcodea. Se lee exclusivamente de variables de entorno.
En dry-run mode, la firma se simula sin necesidad de wallet real.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# EIP-712 domain para Polymarket CLOB
EIP712_DOMAIN = {
    "name": "ClobAuthDomain",
    "version": "1",
    "chainId": 137,  # Polygon mainnet
}

# Tipo de mensaje para autenticación
EIP712_AUTH_TYPE = {
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "int256"},
        {"name": "message", "type": "string"},
    ]
}


class PolymarketAuth:
    """
    Maneja autenticación con Polymarket CLOB.

    Soporta dos modos:
    - dry_run=True: simula firmas, no necesita wallet real
    - dry_run=False: firma real con EIP-712, requiere PRIVATE_KEY en env
    """

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.api_key = os.getenv("POLYMARKET_API_KEY", "")
        self.api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        self.api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        self.wallet_address = os.getenv("POLYMARKET_WALLET_ADDRESS", "")

        if not dry_run:
            self._private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
            if not self._private_key:
                raise ValueError(
                    "POLYMARKET_PRIVATE_KEY requerido en variables de entorno para live/paper trading. "
                    "Usa dry_run=True para testear sin wallet."
                )
            self._validate_credentials()
            self._web3_account = self._load_account()
        else:
            self._private_key = ""
            self._web3_account = None
            logger.info("auth.dry_run_mode", message="Modo dry-run: firmas simuladas")

    def _validate_credentials(self) -> None:
        """Valida que las credenciales necesarias estén presentes."""
        missing = []
        if not self.api_key:
            missing.append("POLYMARKET_API_KEY")
        if not self.api_secret:
            missing.append("POLYMARKET_API_SECRET")
        if not self.wallet_address:
            missing.append("POLYMARKET_WALLET_ADDRESS")

        if missing:
            raise ValueError(
                f"Variables de entorno faltantes: {', '.join(missing)}"
            )

    def _load_account(self) -> Any:
        """Carga la cuenta web3 desde la private key."""
        try:
            from eth_account import Account
            account = Account.from_key(self._private_key)
            logger.info(
                "auth.account_loaded",
                address=account.address,
                matches_env=account.address.lower() == self.wallet_address.lower(),
            )
            return account
        except ImportError:
            raise ImportError(
                "eth-account requerido para live mode. "
                "Instalar: pip install eth-account"
            )

    def get_api_key_headers(self) -> dict[str, str]:
        """
        Headers para autenticación Level 1 (API Key).
        Usados en endpoints de lectura y gestión de órdenes.
        """
        if self.dry_run:
            return {
                "POLY_API_KEY": "dry-run-key",
                "POLY_SECRET": "dry-run-secret",
                "POLY_PASSPHRASE": "dry-run-passphrase",
                "POLY_TIMESTAMP": str(int(time.time())),
            }

        timestamp = str(int(time.time()))
        return {
            "POLY_API_KEY": self.api_key,
            "POLY_SECRET": self.api_secret,
            "POLY_PASSPHRASE": self.api_passphrase,
            "POLY_TIMESTAMP": timestamp,
        }

    def sign_l2_auth(self, nonce: int | None = None) -> dict[str, str]:
        """
        Genera firma EIP-712 para autenticación Level 2.
        Requerida para crear/cancelar órdenes y ver posiciones.

        Returns dict con timestamp, nonce y signature para incluir en headers.
        """
        timestamp = str(int(time.time()))
        nonce = nonce or 0
        message = "This message attests that I control the given wallet"

        if self.dry_run:
            return {
                "POLY_ADDRESS": self.wallet_address or "0x0000000000000000000000000000000000000000",
                "POLY_SIGNATURE": "0x" + "0" * 130,
                "POLY_TIMESTAMP": timestamp,
                "POLY_NONCE": str(nonce),
            }

        try:
            from eth_account.structured_data.hashing import hash_domain, hash_message
            from eth_account._utils.structured_data.hashing import hash_struct

            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                    ],
                    **EIP712_AUTH_TYPE,
                },
                "primaryType": "ClobAuth",
                "domain": EIP712_DOMAIN,
                "message": {
                    "address": self.wallet_address,
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "message": message,
                },
            }

            from eth_account import Account
            signed = Account.sign_typed_data(
                self._private_key,
                full_message=typed_data,
            )

            return {
                "POLY_ADDRESS": self.wallet_address,
                "POLY_SIGNATURE": signed.signature.hex(),
                "POLY_TIMESTAMP": timestamp,
                "POLY_NONCE": str(nonce),
            }

        except Exception as e:
            logger.error("auth.sign_l2_failed", error=str(e))
            raise

    def sign_order(self, order_data: dict[str, Any]) -> str:
        """
        Firma una orden usando EIP-712.

        En dry_run devuelve firma simulada.
        En live mode firma con la private key real.
        """
        if self.dry_run:
            # Firma simulada reproducible para testing
            payload = json.dumps(order_data, sort_keys=True)
            fake_sig = "0x" + hashlib.sha256(payload.encode()).hexdigest() * 2
            return fake_sig[:132]  # 65 bytes = 130 hex chars + 0x

        try:
            from eth_account import Account

            # Construir el typed data de la orden según el formato Polymarket
            typed_data = self._build_order_typed_data(order_data)
            signed = Account.sign_typed_data(
                self._private_key,
                full_message=typed_data,
            )
            return signed.signature.hex()
        except Exception as e:
            logger.error("auth.sign_order_failed", error=str(e), order=order_data)
            raise

    def _build_order_typed_data(self, order: dict[str, Any]) -> dict[str, Any]:
        """
        Construye el typed data EIP-712 para una orden Polymarket.

        Estructura según especificación oficial del CLOB.
        """
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": "Polymarket CTF Exchange",
                "version": "1",
                "chainId": 137,
                "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
            },
            "message": order,
        }

    def generate_hmac_signature(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        Genera firma HMAC para endpoints que la requieren.
        Alternativa a EIP-712 para autenticación de API.
        """
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + (body or "")

        if self.dry_run:
            return {
                "POLY_TIMESTAMP": timestamp,
                "POLY_SIGNATURE": "dry-run-hmac-sig",
            }

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "POLY_TIMESTAMP": timestamp,
            "POLY_SIGNATURE": signature,
        }
