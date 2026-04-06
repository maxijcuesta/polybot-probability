#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — Arranca el bot en modo paper con auto-reinicio (macOS / Linux)
#
# Variables de entorno:
#   POLYBOT_DB_PATH   ruta a la DB SQLite  (default: ./data/polybot.db)
#   POLYBOT_CONFIG    ruta al config TOML  (default: config.toml si existe)
#
# Uso:
#   ./scripts/run.sh            # foreground, Ctrl+C para detener
#   ./scripts/run.sh --bg       # background (nohup + logs a ./logs/bot.log)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/bot.log"
PID_FILE="$LOG_DIR/bot.pid"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# ── Modo background ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--bg" ]]; then
    nohup "$0" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Bot corriendo en background (PID $(cat "$PID_FILE"))"
    echo "  Logs : tail -f $LOG_FILE"
    echo "  Stop : ./scripts/stop.sh"
    exit 0
fi

# ── Señales de stop ───────────────────────────────────────────────────────────
cleanup() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot detenido."
    [[ -n "${BOT_PID:-}" ]] && kill "$BOT_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── Anti-sleep en macOS ───────────────────────────────────────────────────────
if command -v caffeinate &>/dev/null && [[ "${1:-}" != "--no-caffeinate" ]]; then
    caffeinate -si "$0" --no-caffeinate "$@" &
    wait
    exit 0
fi

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH="${POLYBOT_DB_PATH:-$PROJECT_DIR/data/polybot.db}"
export POLYBOT_DB_PATH="$DB_PATH"

BOT_ARGS="--db $DB_PATH"
if [[ -n "${POLYBOT_CONFIG:-}" ]]; then
    BOT_ARGS="$BOT_ARGS --config $POLYBOT_CONFIG"
elif [[ -f "$PROJECT_DIR/config.toml" ]]; then
    BOT_ARGS="$BOT_ARGS --config $PROJECT_DIR/config.toml"
fi

echo "════════════════════════════════════════════"
echo "  polybot-probability — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  DB  : $DB_PATH"
echo "════════════════════════════════════════════"

# ── Loop de auto-reinicio con backoff exponencial ─────────────────────────────
RESTART_DELAY=10
MAX_DELAY=300
FAIL_COUNT=0

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando (intento $((FAIL_COUNT + 1)))..."

    # shellcheck disable=SC2086
    python3 run_bot.py $BOT_ARGS &
    BOT_PID=$!
    echo "$BOT_PID" > "$PID_FILE"

    wait "$BOT_PID"
    EXIT_CODE=$?

    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Detenido limpiamente (código 0)."
        break
    fi

    FAIL_COUNT=$((FAIL_COUNT + 1))
    EXP=$((FAIL_COUNT < 6 ? FAIL_COUNT - 1 : 5))
    DELAY=$(( RESTART_DELAY * (1 << EXP) ))
    [[ $DELAY -gt $MAX_DELAY ]] && DELAY=$MAX_DELAY

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Crash (código $EXIT_CODE). Reintentando en ${DELAY}s..."
    sleep "$DELAY"
done

rm -f "$PID_FILE"
