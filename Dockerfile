# RotaKey v6 — Production Dockerfile
# Build:  docker build -t rotakey:latest .
# Run:    docker run -p 8765:8765 -v $(pwd)/rotakey.yaml:/app/rotakey.yaml rotakey:latest

FROM python:3.12-slim

# Security: don't run as root
RUN groupadd -r rotakey && useradd -r -g rotakey -d /app -s /sbin/nologin rotakey

WORKDIR /app

# Install deps first (cached layer — only re-runs when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY proxy.py .

# Config and state are mounted at runtime — never baked into the image.
# rotakey.yaml  → -v /path/to/rotakey.yaml:/app/rotakey.yaml
# rotakey_state.json is written by the proxy at runtime to /app/data/

RUN mkdir -p /app/data && chown rotakey:rotakey /app/data

USER rotakey

# Expose the default port (override with ROTAKEY_PORT env var)
EXPOSE 8765

# Health check — Docker marks container unhealthy if this fails
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')" || exit 1

CMD ["python3", "proxy.py"]
