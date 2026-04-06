#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_cloud.sh — Entrypoint para Docker / Fly.io
#
# Corre el bot + dashboard en un solo proceso (run_bot.py los inicia juntos).
# Si el proceso termina con error, Docker/Fly.io reinicia el contenedor.
#
# Variables de entorno esperadas:
#   POLYBOT_DB_PATH   ruta a la DB SQLite  (default: /data/polybot.db)
#   POLYBOT_CONFIG    ruta al config TOML  (opcional)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DB_PATH="${POLYBOT_DB_PATH:-/data/polybot.db}"
export POLYBOT_DB_PATH="$DB_PATH"

# Asegurar que el directorio de la DB existe (el volumen debe estar montado)
mkdir -p "$(dirname "$DB_PATH")"

echo "════════════════════════════════════════════"
echo "  polybot-probability — cloud startup"
echo "  DB  : $DB_PATH"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════"

# Construir argumentos
# POLYBOT_CONFIG puede apuntar a config.toml personalizado; si no, usar config.cloud.toml
CONFIG="${POLYBOT_CONFIG:-/app/config.cloud.toml}"
BOT_ARGS="--db $DB_PATH --config $CONFIG"

# Ejecutar — bot + dashboard en el mismo proceso
# run_bot.py arranca el dashboard en 0.0.0.0:8080 y el loop en paralelo
# shellcheck disable=SC2086
exec python3 run_bot.py $BOT_ARGS
