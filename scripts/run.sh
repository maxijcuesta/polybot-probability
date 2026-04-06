#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — Arranca el bot con auto-reinicio y sin apagado del Mac
#
# Qué hace:
#   1. Impide que macOS entre en suspensión mientras el bot está corriendo
#   2. Si el bot crashea, espera unos segundos y lo reinicia automáticamente
#   3. Guarda logs en ./logs/bot.log (rotación automática semanal)
#
# Uso:
#   ./scripts/run.sh            # foreground, Ctrl+C para detener
#   ./scripts/run.sh --bg       # background (nohup), para cerrar la terminal
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/bot.log"
PID_FILE="$PROJECT_DIR/logs/bot.pid"

cd "$PROJECT_DIR"

# ── Background mode ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--bg" ]]; then
    echo "Arrancando en background..."
    nohup "$0" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "✅  Bot corriendo en background (PID $(cat $PID_FILE))"
    echo "    Logs:  tail -f $LOG_FILE"
    echo "    Stop:  ./scripts/stop.sh"
    exit 0
fi

# ── Stop handler ─────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot detenido manualmente."
    # Matar el proceso Python hijo si existe
    [[ -n "${BOT_PID:-}" ]] && kill "$BOT_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── Cabecera de log ───────────────────────────────────────────────────────────
echo "════════════════════════════════════════════"
echo "  probabilisticobot — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Proyecto: $PROJECT_DIR"
echo "  Log: $LOG_FILE"
echo "════════════════════════════════════════════"

# ── Anti-sleep (macOS caffeinate) ─────────────────────────────────────────────
# caffeinate -s = impide que el sistema entre en suspensión
# caffeinate -i = impide suspensión por inactividad
# Solo disponible en macOS — en Linux se ignora
if command -v caffeinate &>/dev/null; then
    echo "☕  caffeinate activado — el Mac no entrará en suspensión"
    caffeinate -si "$0" --internal &
    CAFFEINATE_PID=$!
else
    echo "⚠️   caffeinate no disponible (no es macOS)"
fi

# Si --internal fue pasado por caffeinate, continuar normalmente
[[ "${1:-}" == "--internal" ]] && shift || true

# ── Loop de auto-reinicio ─────────────────────────────────────────────────────
RESTART_DELAY=10   # segundos antes de reintentar
MAX_DELAY=300       # máximo 5 minutos de espera
FAIL_COUNT=0

while true; do
    echo ""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ▶  Iniciando bot (intento $((FAIL_COUNT + 1)))..."

    # Arrancar el bot y guardar su PID
    python3 -m app.main &
    BOT_PID=$!
    echo $BOT_PID > "$PID_FILE"

    # Esperar a que termine
    wait "$BOT_PID"
    EXIT_CODE=$?

    # Exit limpio (código 0) = detención intencional, no reiniciar
    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅  Bot terminó limpiamente (código 0). No se reiniciará."
        break
    fi

    # Crash o error
    FAIL_COUNT=$((FAIL_COUNT + 1))
    # Backoff exponencial: 10s, 20s, 40s, ... hasta MAX_DELAY
    DELAY=$(( RESTART_DELAY * (1 << (FAIL_COUNT - 1)) ))
    [[ $DELAY -gt $MAX_DELAY ]] && DELAY=$MAX_DELAY

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️   Bot terminó con error (código $EXIT_CODE). Reintentando en ${DELAY}s..."
    sleep "$DELAY"
done

rm -f "$PID_FILE"
