
<div align="center">
  
  # RotaKey
<img width="769" height="236" alt="Screenshot 2026-04-04 191052" src="https://github.com/user-attachments/assets/2942899c-1de8-4e9b-81ce-aad71bfc77fb" /> 

**Smart API key rotation and model fallback proxy for OpenRouter, Anthropic, OpenAI, and Gemini.**

RotaKey sits between your app and upstream AI providers. When a key hits a rate limit, it automatically tries the next model, then the next key, then the next provider — transparently, with no changes needed in your client code.

```
Your app  →  http://localhost:8765/openrouter/v1/chat/completions
                          ↓
                      RotaKey
                ┌─────────────────┐
                │  key-1 / model-a │ → 429 → try model-b
                │  key-1 / model-b │ → 429 → rotate key
                │  key-2 / model-a │ → 200 ✓
                └─────────────────┘
```

---
</div>
## Quickstart (2 minutes)

### Option A — Python

```bash
git clone https://github.com/seph1709/rotakey
cd rotakey

# Install (Linux/macOS)
./install.sh

# Add your API keys to .env (created by install.sh)
echo 'ROTAKEY_KEYS_OPENROUTER=sk-or-v1-your-key-here' >> .env

# Start
./start.sh
```

```bat
# Windows
install.bat
# Edit .env, then:
start.bat
```

### Option B — Docker (recommended for teams)

```bash
git clone https://github.com/seph1709/rotakey
cd rotakey

# Copy and edit .env
cp .env.example .env
# Add: ROTAKEY_KEYS_OPENROUTER=sk-or-v1-your-key-here

docker compose up -d
```

### Verify it's running

```bash
curl http://localhost:8765/health
# → {"status":"ok"}

curl http://localhost:8765/status
# → JSON with per-key cooldown state
```

---

## Client configuration

Point any OpenAI-compatible client at RotaKey:

```bash
export OPENAI_BASE_URL=http://localhost:8765/openrouter
export OPENAI_API_KEY=rotakey          # or your ROTAKEY_TOKEN value
```

**Per provider:**

| Provider   | Base URL                                   |
|------------|--------------------------------------------|
| OpenRouter | `http://localhost:8765/openrouter/...`     |
| Anthropic  | `http://localhost:8765/anthropic/...`      |
| OpenAI     | `http://localhost:8765/openai/...`         |
| Gemini     | `http://localhost:8765/gemini/...`         |

**Force a specific provider** via model namespace prefix:

```json
{ "model": "rotakey/anthropic/claude-haiku-4-5-20251001" }
```

---

## API keys — recommended approach

Never hardcode keys in `rotakey.yaml`. Use environment variables instead:

```bash
# .env (chmod 600, never commit)

# All keys for a provider, comma-separated:
ROTAKEY_KEYS_OPENROUTER=sk-or-v1-aaa,sk-or-v1-bbb,sk-or-v1-ccc
ROTAKEY_KEYS_ANTHROPIC=sk-ant-xxx

# Or indexed (useful with secrets managers):
ROTAKEY_KEY_OPENROUTER_1=sk-or-v1-aaa
ROTAKEY_KEY_OPENROUTER_2=sk-or-v1-bbb
```

Keys from environment are merged with any keys in `rotakey.yaml` (env keys are appended, no duplicates).

**Kubernetes / Docker secrets:** Mount the secret as an env var in your `deployment.yaml` or `docker-compose.yml`. The proxy reads it on each config reload (every `keys_cache_ttl` seconds).

---

## Configuration reference (`rotakey.yaml`)

```yaml
server:
  host: "127.0.0.1"    # Use "0.0.0.0" inside Docker
  port: 8765            # Override: ROTAKEY_PORT env var
  log_file: "rotakey.log"
  log_level: "INFO"     # DEBUG | INFO | WARNING | ERROR
  log_format: "text"    # "text" (colored) | "json" (for Datadog/Loki)

http_client:
  timeout_connect: 10.0
  timeout_read: 120.0   # Increase for large models / long outputs
  timeout_write: 30.0
  max_connections: 20

rate_limit:
  backoff_schedule: [60, 120, 300, 600]  # seconds per 429 hit
  recovery_window: 300    # seconds before hit_count resets
  invalid_key_cooldown: 86400  # 24h on 401/403
  keys_cache_ttl: 30      # hot-reload interval (seconds)

# Map client model names to provider-specific names on spillover:
model_map:
  "claude-sonnet-4":
    anthropic:  "claude-sonnet-4-20250514"
    openrouter: "anthropic/claude-sonnet-4"
```

**Hot reload:** Edit `rotakey.yaml` while the proxy is running. Changes are picked up within `keys_cache_ttl` seconds — no restart needed.

---

## Environment variable reference

| Variable | Description | Default |
|---|---|---|
| `ROTAKEY_PORT` | Override listen port | `8765` |
| `ROTAKEY_TOKEN` | Require `Authorization: Bearer <token>` on all requests | *(disabled)* |
| `ROTAKEY_KEYS_<PROVIDER>` | Comma-separated keys for a provider | *(from yaml)* |
| `ROTAKEY_KEY_<PROVIDER>_N` | Indexed key (N = 1, 2, …) | *(from yaml)* |

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Always returns `{"status":"ok"}`. No auth required. |
| `GET /status` | Per-provider, per-key cooldown state in JSON. |
| `GET /metrics` | Prometheus text exposition format. |
| `ANY /<provider>/...` | Proxied to the upstream provider. |

### Prometheus metrics

Scrape `http://localhost:8765/metrics`. Available metrics:

| Metric | Type | Labels |
|---|---|---|
| `rotakey_requests_total` | counter | `provider`, `status` |
| `rotakey_429_total` | counter | `provider`, `key_hint`, `model` |
| `rotakey_errors_total` | counter | `provider`, `error_type` |
| `rotakey_request_duration_ms_sum` | counter | `provider` |
| `rotakey_request_duration_ms_count` | counter | `provider` |
| `rotakey_keys_active` | gauge | `provider` |
| `rotakey_keys_total` | gauge | `provider` |

See `prometheus.yml` for a ready-to-use scrape config.

---

## How rotation works

1. **Model fallback** — on a 429 or 5xx, try the next model in `model_fallback.chain` (same key, same provider).
2. **Key rotation** — once all models are rate-limited on a key, move to the next key.
3. **Provider spillover** — once all keys on a provider are exhausted (all 429s), spill to the next provider in the `providers:` list. Model names are rewritten via `model_map`.
4. **Circuit breaker** — if a provider returns 5+ consecutive timeouts/5xx, it's skipped for 60 seconds.
5. **State persistence** — cooldown state is saved to `rotakey_state.json` and loaded on restart so warmup doesn't reset rate-limit tracking.

---

## Production deployment

### Linux systemd service

```bash
sudo useradd -r -s /sbin/nologin rotakey
sudo mkdir -p /opt/rotakey
sudo cp proxy.py rotakey.yaml requirements.txt /opt/rotakey/
sudo pip3 install -r /opt/rotakey/requirements.txt

sudo cp rotakey.service /etc/systemd/system/
sudo mkdir -p /etc/rotakey
sudo cp .env.example /etc/rotakey/rotakey.env
# Edit /etc/rotakey/rotakey.env with your keys
sudo chmod 600 /etc/rotakey/rotakey.env

sudo systemctl daemon-reload
sudo systemctl enable --now rotakey
sudo systemctl status rotakey
```

### Docker with auto-restart

```bash
docker compose up -d
docker compose logs -f rotakey
```

---

## Security

- The proxy **only listens on `127.0.0.1`** (localhost) by default — not reachable from the network.
- Set `ROTAKEY_TOKEN` to require authentication from local clients (other processes on the same machine).
- `rotakey.yaml` and `.env` are automatically set to `chmod 600` by the installer.
- API keys are **never logged** — only a hint (`sk-or-v1-f6d1d44b...417e`) appears in logs.
- In Docker, the proxy runs as a non-root `rotakey` user.

---

## CLI reference

```bash
python3 proxy.py               # start the proxy
python3 proxy.py --validate    # validate config and exit
python3 proxy.py --dry-run     # validate + probe upstream connectivity
python3 proxy.py --check       # alias for --validate
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v          # run test suite
ruff check proxy.py       # lint
```

Tests use `respx` to mock upstream responses — no real API keys needed.

---

## Upgrading

1. Pull the new `proxy.py`.
2. Check `requirements.txt` for any version bumps and re-run `pip install -r requirements.txt`.
3. Run `python3 proxy.py --validate` to catch any new required config fields.
4. Restart.

State files from previous versions are forward-compatible.

---

## License

MIT