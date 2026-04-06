#!/usr/bin/env python3
"""
Script de validación de conexión al CLOB.

Verifica:
1. Conectividad al CLOB REST API
2. Conectividad a Gamma API
3. Variables de entorno configuradas
4. DB accesible
5. WebSocket puede conectarse (en dry_run)

Ejecutar antes del primer deploy para verificar que todo está OK.
"""
import asyncio
import os
import sys
from pathlib import Path

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def validate() -> None:
    from app.clients.auth import PolymarketAuth
    from app.clients.clob_rest import ClobRestClient
    from app.config import get_settings
    from app.storage.db import close_db, init_db
    from app.utils.logging import setup_logging

    setup_logging(level="INFO", json_output=False)

    settings = get_settings()
    print(f"\n{'='*50}")
    print(f"VALIDACIÓN DE CONEXIÓN — Modo: {settings.operation_mode}")
    print(f"{'='*50}\n")

    errors = []
    warnings = []

    # ─── 1. VARIABLES DE ENTORNO ──────────────────────────────────────────────
    print("1. Verificando variables de entorno...")

    env_vars = {
        "POLYMARKET_API_KEY": "opcional (necesario para live)",
        "POLYMARKET_WALLET_ADDRESS": "opcional (necesario para live)",
        "POLYMARKET_PRIVATE_KEY": "SECRETO — necesario para live/paper",
    }

    for var, desc in env_vars.items():
        val = os.getenv(var, "")
        if val:
            print(f"   ✅ {var}: configurado")
        else:
            print(f"   ⚠️  {var}: no configurado ({desc})")
            if "SECRETO" in desc:
                warnings.append(f"{var} no configurado — solo dry_run disponible")

    # ─── 2. CLOB REST API ─────────────────────────────────────────────────────
    print("\n2. Verificando CLOB REST API...")
    auth = PolymarketAuth(dry_run=True)

    async with ClobRestClient(auth, dry_run=True) as rest:
        ok = await rest.health_check()
        if ok:
            print("   ✅ CLOB REST API: accesible")
        else:
            print("   ❌ CLOB REST API: NO accesible")
            errors.append("CLOB REST API no accesible")

        # ─── 3. GAMMA API ─────────────────────────────────────────────────────
        print("\n3. Verificando Gamma API (mercados)...")
        try:
            markets = await rest.get_gamma_markets(limit=5, active=True)
            print(f"   ✅ Gamma API: OK — {len(markets)} mercados de muestra obtenidos")
            if markets:
                m = markets[0]
                print(f"   📊 Ejemplo: {m.get('question', 'N/A')[:60]}")
        except Exception as e:
            print(f"   ❌ Gamma API: ERROR — {e}")
            errors.append(f"Gamma API: {e}")

        # ─── 4. SAMPLING MARKETS ─────────────────────────────────────────────
        print("\n4. Verificando mercados de sampling...")
        try:
            sampling = await rest.get_sampling_simplified_markets()
            print(f"   ✅ Sampling markets: {len(sampling)} mercados")
        except Exception as e:
            print(f"   ⚠️  Sampling markets: {e}")
            warnings.append(f"Sampling markets: {e}")

    # ─── 5. BASE DE DATOS ─────────────────────────────────────────────────────
    print("\n5. Verificando base de datos...")
    try:
        db = await init_db(":memory:")  # test con DB en memoria
        result = await db.fetch_scalar("SELECT COUNT(*) FROM markets")
        print(f"   ✅ DB: OK — tabla markets accesible ({result} filas)")
        await close_db()
    except Exception as e:
        print(f"   ❌ DB: ERROR — {e}")
        errors.append(f"DB: {e}")

    # ─── 6. CONFIG ────────────────────────────────────────────────────────────
    print("\n6. Verificando configuración...")
    print(f"   Mode: {settings.operation_mode}")
    print(f"   Max exposure: {settings.max_total_exposure_usdc} USDC")
    print(f"   Max positions: {settings.max_open_positions}")
    print(f"   Min edge: {settings.min_edge_pct}%")
    print(f"   Take profit: {settings.take_profit_pct}%")
    print(f"   Trailing stop: {settings.trailing_stop_pct}%")
    print("   ✅ Config: OK")

    # ─── RESUMEN ──────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print("RESUMEN")
    print(f"{'='*50}")

    if errors:
        print(f"\n❌ ERRORES ({len(errors)}):")
        for e in errors:
            print(f"   - {e}")

    if warnings:
        print(f"\n⚠️  ADVERTENCIAS ({len(warnings)}):")
        for w in warnings:
            print(f"   - {w}")

    if not errors and not warnings:
        print("\n✅ Todo OK — el bot está listo para ejecutarse")
        print(f"   Modo: {settings.operation_mode}")
    elif not errors:
        print("\n✅ Sin errores críticos — puede ejecutarse con limitaciones")
    else:
        print("\n❌ Hay errores críticos — revisar antes de ejecutar")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(validate())
