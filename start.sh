#!/bin/bash
# RotaKey v6 — Start script

# Load .env if present (won't override already-exported vars)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Pass --dry-run or --validate through to the proxy
if [[ "$*" == *"--dry-run"* ]] || [[ "$*" == *"--validate"* ]]; then
    exec python3 proxy.py "$@"
fi

echo ""
echo " =========================================="
echo "   RotaKey Proxy v6 - Starting..."
echo " =========================================="
echo ""
echo " Default port : ${ROTAKEY_PORT:-8765}"
echo " Health check : http://localhost:${ROTAKEY_PORT:-8765}/health"
echo " Status page  : http://localhost:${ROTAKEY_PORT:-8765}/status"
echo " Metrics      : http://localhost:${ROTAKEY_PORT:-8765}/metrics"
echo " Logs         : rotakey.log (in this folder)"
echo ""
if [ -n "$ROTAKEY_TOKEN" ]; then
    echo " Auth         : ENABLED (ROTAKEY_TOKEN is set)"
else
    echo " Auth         : DISABLED -- set ROTAKEY_TOKEN in .env to enable"
fi
echo ""
echo " Client env:"
echo "   OPENAI_BASE_URL = http://localhost:${ROTAKEY_PORT:-8765}/openrouter"
echo "   OPENAI_API_KEY  = ${ROTAKEY_TOKEN:-rotakey}"
echo ""
echo " Press Ctrl+C to stop."
echo " =========================================="
echo ""

exec python3 proxy.py
