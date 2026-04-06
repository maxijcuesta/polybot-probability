#!/usr/bin/env bash
# stop.sh — Detiene el bot que corre en background

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/../logs/bot.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "⚠️  No se encontró bot corriendo en background (no hay $PID_FILE)"
    # Intentar encontrarlo por nombre de proceso
    PIDS=$(pgrep -f "app.main" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        echo "   Se encontraron procesos Python del bot: $PIDS"
        echo "   Para detenerlos: kill $PIDS"
    fi
    exit 1
fi

PID=$(cat "$PID_FILE")
echo "Deteniendo bot (PID $PID)..."
kill "$PID" 2>/dev/null && echo "✅  Bot detenido." || echo "⚠️  Proceso no encontrado (puede que ya haya terminado)."
rm -f "$PID_FILE"
