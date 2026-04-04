#!/bin/bash
# RotaKey v6 — Linux/macOS Installer

set -e

echo ""
echo " =========================================="
echo "   RotaKey Proxy v6 - Linux Installer"
echo " =========================================="
echo ""

# ── Python check ────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
    echo " [ERROR] Python 3.10+ is required. Install with:"
    echo "   sudo apt install python3 python3-pip   # Debian/Ubuntu"
    echo "   sudo dnf install python3               # Fedora/RHEL"
    exit 1
fi

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMIN=$(python3 -c "import sys; print(1 if sys.version_info >= (3,10) else 0)")
echo " [OK] Python ${PYVER} found"

if [ "$PYMIN" = "0" ]; then
    echo " [ERROR] Python 3.10 or newer required (found ${PYVER})"
    exit 1
fi

# ── Install pinned deps ──────────────────────────────────────
echo ""
echo " Installing pinned dependencies from requirements.txt..."
echo ""

if [ ! -f requirements.txt ]; then
    echo " [ERROR] requirements.txt not found next to install.sh"
    exit 1
fi

pip3 install -r requirements.txt --break-system-packages -q 2>/dev/null \
  || pip3 install -r requirements.txt -q

echo " [OK] Dependencies installed (pinned versions)"

# ── File permissions ─────────────────────────────────────────
chmod +x start.sh

if [ -f rotakey.yaml ]; then
    chmod 600 rotakey.yaml
    echo " [OK] rotakey.yaml set to chmod 600 (owner only)"
fi

# ── .env setup ───────────────────────────────────────────────
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    chmod 600 .env
    echo " [OK] Created .env from .env.example — edit it to add your keys"
fi

# ── Validate config ──────────────────────────────────────────
echo ""
echo " Validating configuration..."
if python3 proxy.py --validate; then
    echo " [OK] Config valid"
else
    echo " [WARN] Config validation failed — check rotakey.yaml or .env"
fi

echo ""
echo " =========================================="
echo "   SECURITY NOTICE"
echo " =========================================="
echo ""
echo " rotakey.yaml stores settings as plain text."
echo " API keys should be in .env (auto-created above)."
echo " chmod 600 has been applied to both files."
echo " Keep this folder out of git / cloud sync."
echo ""
echo " Set ROTAKEY_TOKEN to require clients to authenticate:"
echo "   echo 'ROTAKEY_TOKEN=your-secret' >> .env"
echo ""
echo " =========================================="
echo "   Installation complete!"
echo " =========================================="
echo ""
echo " Next steps:"
echo "   1. Edit .env — add your API keys:"
echo "      ROTAKEY_KEYS_OPENROUTER=sk-or-v1-..."
echo "   2. Test connectivity:   ./start.sh --dry-run"
echo "   3. Start the proxy:     ./start.sh"
echo ""
echo "   Or with Docker:"
echo "      docker compose up -d"
echo ""
echo " Client env settings:"
echo "   OPENAI_BASE_URL = http://localhost:8765/openrouter"
echo "   OPENAI_API_KEY  = rotakey   (or your ROTAKEY_TOKEN)"
echo ""
echo " Provider routing:"
echo "   OpenRouter -->  http://localhost:8765/openrouter/..."
echo "   Anthropic  -->  http://localhost:8765/anthropic/..."
echo "   OpenAI     -->  http://localhost:8765/openai/..."
echo "   Gemini     -->  http://localhost:8765/gemini/..."
echo ""
