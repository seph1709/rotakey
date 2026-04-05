<div align="center">

# RotaKey

<img width="769" height="236" alt="RotaKey banner" src="assets/banner.png" />

**Smart API key rotation and model fallback proxy for OpenRouter, Anthropic, OpenAI, and Gemini.**

[![CI](https://github.com/seph1709/rotakey/actions/workflows/ci.yml/badge.svg)](https://github.com/seph1709/rotakey/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/seph1709/rotakey/pulls)
[![OpenAI Compatible](https://img.shields.io/badge/OpenAI-compatible-412991?logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference)

RotaKey sits between your app and upstream AI providers. When a key hits a rate limit, it automatically tries the next model, then the next key, then the next provider — **transparently, with zero changes to your client code.**

| Step | What happens |
|------|-------------|
| Your app sends a request | `POST http://localhost:8765/openrouter/v1/chat/completions` |
| key-1 / model-a hits 429 | RotaKey tries next model |
| key-1 / model-b hits 429 | RotaKey rotates to next key |
| key-2 / model-a returns 200 | Request succeeds — your app gets the response |

</div>

---

## Why RotaKey?

| Problem | RotaKey fix |
|---|---|
| Single key hits rate limits | Rotates across multiple keys automatically |
| Model goes down or 5xx | Falls back to next model in chain |
| One provider is overloaded | Spills over to the next provider |
| Keys burned after restart | Persists cooldown state across restarts |
| Need observability | Built-in Prometheus metrics |

---

## Quickstart

### Option A — Python (Linux / macOS / Windows)

```bash
git clone https://github.com/seph1709/rotakey
cd rotakey

# Linux/macOS
./install.sh
echo 'ROTAKEY_KEYS_OPENROUTER=sk-or-v1-your-key-here' >> .env
./start.sh
```

```bat
:: Windows
install.bat
:: Edit .env, then:
start.bat
```

### Option B — Docker (recommended for teams)

```bash
git clone https://github.com/seph1709/rotakey
cd rotakey
cp .env.example .env
# Add your keys to .env
docker compose up -d
```

### Verify

```bash
curl http://localhost:8765/health
# → {"status":"ok"}

curl http://localhost:8765/status
# → JSON with per-key cooldown state
```

---

## Supported Providers

| Provider | Base URL | Auth Header |
|---|---|---|
| OpenRouter | `http://localhost:8765/openrouter/...` | `Authorization: Bearer` |
| Anthropic | `http://localhost:8765/anthropic/...` | `x-api-key` |
| OpenAI | `http://localhost:8765/openai/...` | `Authorization: Bearer` |
| Gemini | `http://localhost:8765/gemini/...` | `x-goog-api-key` |

---

## Client Configuration

Point **any OpenAI-compatible client** at RotaKey — no SDK changes needed:

```bash
export OPENAI_BASE_URL=http://localhost:8765/openrouter
export OPENAI_API_KEY=rotakey    # or your ROTAKEY_TOKEN value
```

**Force a specific provider** via model namespace prefix:

```json
{ "model": "rotakey/anthropic/claude-haiku-4-5-20251001" }
```

---

## API Keys

Never hardcode keys in `rotakey.yaml`. Use environment variables:

```bash
# .env  (chmod 600 — never commit)

# Comma-separated keys per provider:
ROTAKEY_KEYS_OPENROUTER=sk-or-v1-aaa,sk-or-v1-bbb,sk-or-v1-ccc
ROTAKEY_KEYS_ANTHROPIC=sk-ant-xxx

# Or indexed (great for secrets managers / Vault):
ROTAKEY_KEY_OPENROUTER_1=sk-or-v1-aaa
ROTAKEY_KEY_OPENROUTER_2=sk-or-v1-bbb
```

Env keys are merged with `rotakey.yaml` — no duplicates. Hot-reloaded every `keys_cache_ttl` seconds.

**Kubernetes / Docker secrets:** Mount as env vars in `deployment.yaml` or `docker-compose.yml`.

---

## Configuration (`rotakey.yaml`)

```yaml
server:
  host: "127.0.0.1"    # "0.0.0.0" inside Docker
  port: 8765            # override: ROTAKEY_PORT
  log_level: "INFO"     # DEBUG | INFO | WARNING | ERROR
  log_format: "text"    # "text" (colored) | "json" (Datadog/Loki)

http_client:
  timeout_connect: 10.0
  timeout_read: 120.0   # increase for large outputs
  max_connections: 20

rate_limit:
  backoff_schedule: [60, 120, 300, 600]  # seconds per 429 hit
  recovery_window: 300
  invalid_key_cooldown: 86400            # 24h on 401/403
  keys_cache_ttl: 30                     # hot-reload interval

model_map:
  "claude-sonnet-4":
    anthropic:  "claude-sonnet-4-20250514"
    openrouter: "anthropic/claude-sonnet-4"
```

> **Hot reload:** Changes to `rotakey.yaml` apply within `keys_cache_ttl` seconds — no restart needed.

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `ROTAKEY_PORT` | Override listen port | `8765` |
| `ROTAKEY_TOKEN` | Require `Authorization: Bearer <token>` on all requests | *(disabled)* |
| `ROTAKEY_KEYS_<PROVIDER>` | Comma-separated keys for a provider | *(from yaml)* |
| `ROTAKEY_KEY_<PROVIDER>_N` | Indexed key (N = 1, 2, …) | *(from yaml)* |

---

## How Rotation Works

```
Request
  │
  ├─ model-a (key-1) → 429
  ├─ model-b (key-1) → 429   ← model fallback (same key)
  ├─ model-a (key-2) → 429
  ├─ model-b (key-2) → 429   ← key rotation
  ├─ provider-2 / model-a  → 200 ✓  ← provider spillover
```

1. **Model fallback** — 429/5xx → try next model in `model_fallback.chain` (same key).
2. **Key rotation** — all models exhausted on a key → next key.
3. **Provider spillover** — all keys exhausted → next provider; model names rewritten via `model_map`.
4. **Circuit breaker** — 5+ consecutive timeouts/5xx → provider skipped for 60 s.
5. **State persistence** — cooldown state saved to `rotakey_state.json`, survives restarts.

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | `{"status":"ok"}` — no auth required. Use for load balancer health checks. |
| `GET /status` | Per-provider, per-key cooldown state in JSON. |
| `GET /metrics` | Prometheus exposition format. |
| `ANY /<provider>/...` | Proxied to the upstream provider. |

### Prometheus Metrics

Scrape `http://localhost:8765/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `rotakey_requests_total` | counter | `provider`, `status` |
| `rotakey_429_total` | counter | `provider`, `key_hint`, `model` |
| `rotakey_errors_total` | counter | `provider`, `error_type` |
| `rotakey_request_duration_ms_sum` | counter | `provider` |
| `rotakey_keys_active` | gauge | `provider` |
| `rotakey_keys_total` | gauge | `provider` |

See [`prometheus.yml`](prometheus.yml) for a ready-to-use scrape config.

---

## Production Deployment

### Linux — systemd

```bash
sudo useradd -r -s /sbin/nologin rotakey
sudo mkdir -p /opt/rotakey
sudo cp proxy.py rotakey.yaml requirements.txt /opt/rotakey/
sudo pip3 install -r /opt/rotakey/requirements.txt

sudo cp rotakey.service /etc/systemd/system/
sudo mkdir -p /etc/rotakey
sudo cp .env.example /etc/rotakey/rotakey.env
sudo chmod 600 /etc/rotakey/rotakey.env
# Edit /etc/rotakey/rotakey.env with your keys

sudo systemctl daemon-reload
sudo systemctl enable --now rotakey
sudo systemctl status rotakey
```

### Docker

```bash
docker compose up -d
docker compose logs -f rotakey
```

---

## Security

- Listens on `127.0.0.1` (localhost) by default — not reachable from the network.
- Set `ROTAKEY_TOKEN` to require auth from local clients.
- `rotakey.yaml` and `.env` are `chmod 600`'d automatically by the installer.
- API keys are **never logged** — only a masked hint appears (`sk-or-v1-f6d1...417e`).
- Docker image runs as a non-root `rotakey` user.

---

## CLI

```bash
python3 proxy.py               # start
python3 proxy.py --validate    # validate config and exit
python3 proxy.py --dry-run     # validate + probe upstream connectivity
python3 proxy.py --check       # alias for --validate
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v          # run tests (no real API keys needed)
ruff check proxy.py       # lint
```

Tests use `respx` to mock upstream responses.

---

## Upgrading

1. Pull the new `proxy.py`.
2. `pip install -r requirements.txt` (pick up any new deps).
3. `python3 proxy.py --validate` — catch any new required config fields.
4. Restart.

State files from older versions are forward-compatible.

---

## Contributing

PRs and issues are welcome. Please open an issue first for significant changes.

---

## License

[MIT](LICENSE)
