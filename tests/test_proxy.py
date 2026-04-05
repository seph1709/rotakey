"""
RotaKey v6 — test suite
========================
Run:  pytest tests/ -v
Deps: pip install -r requirements-dev.txt

Tests use respx to mock upstream provider calls so no real API keys are needed.
The FastAPI test client drives requests; state is fresh per test.
"""

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

# ── Insert project root so proxy module is importable ────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Minimal config used by all tests ─────────────────────────────────────────

MINIMAL_CFG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "log_file": "/tmp/rotakey_test.log",
        "log_level": "WARNING",
        "log_format": "text",
    },
    "http_client": {
        "timeout_connect": 5.0,
        "timeout_read": 10.0,
        "timeout_write": 5.0,
        "timeout_pool": 2.0,
        "max_connections": 5,
        "max_keepalive": 2,
    },
    "rate_limit": {
        "state_file": "/tmp/rotakey_test_state.json",
        "recovery_window": 300,
        "backoff_schedule": [1, 2, 5, 10],
        "invalid_key_cooldown": 3600,
        "keys_cache_ttl": 1,
    },
    "model_map": {},
    "providers": {
        "openrouter": {
            "base_url": "https://openrouter.ai/api",
            "prefix": "/openrouter",
            "key_header": "Authorization",
            "key_prefix": "Bearer ",
            "model_fallback": {
                "trigger_statuses": [429, 500, 502, 503, 404],
                "chain": ["model-a", "model-b"],
            },
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "prefix": "/anthropic",
            "key_header": "x-api-key",
            "extra_headers": {"anthropic-version": "2023-06-01"},
            "model_fallback": {
                "trigger_statuses": [429, 500, 502, 503],
                "chain": [],
            },
        },
    },
    "keys": {
        "openrouter": ["key-alpha", "key-beta"],
        "anthropic":  ["ant-key-1"],
    },
}


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """Reset all in-memory state and create a fresh http client between tests."""
    import httpx as hx

    import proxy as p

    p._rl_state      = {}
    p._key_dead      = {}
    p._last_ok_key   = {}
    p._provider_cb   = {}
    p._m_requests_total.clear()
    p._m_429_total.clear()
    p._m_errors_total.clear()
    p._m_duration_sum.clear()
    p._m_duration_count.clear()

    # Always inject a live client — respx patches AsyncClient.send at class
    # level, so any live instance will have its send intercepted.
    p._http_client = hx.AsyncClient(timeout=10.0)

    # Inject minimal config directly so tests don't touch disk
    p._cfg            = MINIMAL_CFG
    p._cfg_mtime      = time.monotonic()
    p._cfg_last_check = time.monotonic()

    yield

    # Clean up
    sf = Path("/tmp/rotakey_test_state.json")
    if sf.exists():
        sf.unlink()


@pytest.fixture()
def client():
    """Return a synchronous TestClient for the FastAPI app."""
    import proxy as p
    return TestClient(p.app, raise_server_exceptions=False)


# ── /health ───────────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_shape(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body
    assert "openrouter" in body["providers"]
    pdata = body["providers"]["openrouter"]
    assert "keys_total" in pdata
    assert "keys_active" in pdata
    assert pdata["keys_total"] == 2


# ── /metrics ─────────────────────────────────────────────────────────────────

def test_metrics_returns_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Even with zero traffic the keys gauge lines should be present
    assert "rotakey_keys_total" in r.text
    assert "rotakey_keys_active" in r.text


# ── Inbound auth ──────────────────────────────────────────────────────────────

def test_auth_blocks_without_token(monkeypatch):
    import proxy as p
    monkeypatch.setattr(p, "_ROTAKEY_TOKEN", "secret-token")
    with TestClient(p.app, raise_server_exceptions=False) as tc:
        r = tc.get("/status")
    assert r.status_code == 401


def test_auth_allows_health_without_token(monkeypatch):
    import proxy as p
    monkeypatch.setattr(p, "_ROTAKEY_TOKEN", "secret-token")
    with TestClient(p.app, raise_server_exceptions=False) as tc:
        r = tc.get("/health")
    assert r.status_code == 200


def test_auth_passes_with_correct_token(monkeypatch):
    import proxy as p
    monkeypatch.setattr(p, "_ROTAKEY_TOKEN", "secret-token")
    with TestClient(p.app, raise_server_exceptions=False) as tc:
        r = tc.get("/status", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200


# ── Proxy: 200 success path ───────────────────────────────────────────────────

@respx.mock
def test_proxy_success_200(client):
    """A clean upstream 200 response is forwarded to the client."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "cmpl-1", "choices": [{"message": {"content": "hello"}}]},
        )
    )
    r = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json()["id"] == "cmpl-1"


# ── Proxy: model fallback on 429 ──────────────────────────────────────────────

@respx.mock
def test_model_fallback_on_429(client):
    """First model gets a 429 → proxy retries with model-b on the same key."""
    call_count = {"n": 0}

    def side_effect(request):
        call_count["n"] += 1
        body = json.loads(request.content)
        if body.get("model") == "model-a":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        # model-b succeeds
        return httpx.Response(200, json={"id": "cmpl-fb", "model": "model-b"})

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=side_effect
    )
    r = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json()["model"] == "model-b"
    assert call_count["n"] == 2


# ── Proxy: key rotation when all models on first key are cooled ───────────────

@respx.mock
def test_key_rotation_after_all_models_cooled(client):
    """After both models on key-alpha are rate-limited, proxy rotates to key-beta."""
    import proxy as p

    now = time.time()
    p._rl_state = {
        "openrouter": {
            "key-alpha": {
                "model-a": {"cooled_until": now + 300, "hit_count": 1},
                "model-b": {"cooled_until": now + 300, "hit_count": 1},
            }
        }
    }

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "cmpl-beta", "key_used": "key-beta"})
    )
    r = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    # key-beta request should have gone through
    req = respx.calls[0].request
    assert "key-beta" in req.headers.get("authorization", "")


# ── Proxy: 401 marks key dead ─────────────────────────────────────────────────

@respx.mock
def test_401_marks_key_dead(client):
    """A 401 from upstream marks the key dead and rotates to the next key."""
    call_count = {"n": 0}

    def side_effect(request):
        call_count["n"] += 1
        auth = request.headers.get("authorization", "")
        if "key-alpha" in auth:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return httpx.Response(200, json={"id": "cmpl-ok"})

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=side_effect
    )
    r = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    import proxy as p
    assert p._is_key_dead("openrouter", "key-alpha")


# ── Proxy: all providers exhausted returns structured 429 ────────────────────

@respx.mock
def test_all_keys_exhausted_returns_429(client):
    """When every key+model is rate-limited the proxy returns 429 with a body."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )

    r = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "all_keys_exhausted"
    assert "retry_after" in body["error"]


# ── Proxy: body too large ────────────────────────────────────────────────────

def test_body_too_large_returns_413(client):
    big = b"x" * (17 * 1024 * 1024)
    r = client.post(
        "/openrouter/v1/chat/completions",
        content=big,
        headers={"content-type": "application/json", "content-length": str(len(big))},
    )
    assert r.status_code == 413


# ── Env-var key injection ─────────────────────────────────────────────────────

def test_env_var_key_injection_bulk(monkeypatch):
    """ROTAKEY_KEYS_<PROVIDER>=k1,k2 injects into the keys section."""
    monkeypatch.setenv("ROTAKEY_KEYS_OPENROUTER", "sk-injected-1,sk-injected-2")
    import proxy as p

    cfg = {
        "providers": {"openrouter": {}},
        "keys": {"openrouter": ["sk-existing"]},
    }
    p._inject_env_keys(cfg)
    assert "sk-injected-1" in cfg["keys"]["openrouter"]
    assert "sk-injected-2" in cfg["keys"]["openrouter"]
    assert "sk-existing" in cfg["keys"]["openrouter"]
    # No duplicates
    assert len(cfg["keys"]["openrouter"]) == 3


def test_env_var_key_injection_indexed(monkeypatch):
    """ROTAKEY_KEY_<PROVIDER>_N=key injects individual indexed keys."""
    monkeypatch.setenv("ROTAKEY_KEY_ANTHROPIC_1", "sk-ant-injected")
    import proxy as p

    cfg = {
        "providers": {"anthropic": {}},
        "keys": {"anthropic": []},
    }
    p._inject_env_keys(cfg)
    assert "sk-ant-injected" in cfg["keys"]["anthropic"]


def test_env_var_no_duplicates(monkeypatch):
    """Keys already in YAML are not duplicated by env injection."""
    monkeypatch.setenv("ROTAKEY_KEYS_OPENROUTER", "sk-existing,sk-new")
    import proxy as p

    cfg = {
        "providers": {"openrouter": {}},
        "keys": {"openrouter": ["sk-existing"]},
    }
    p._inject_env_keys(cfg)
    assert cfg["keys"]["openrouter"].count("sk-existing") == 1


# ── Circuit breaker ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_threshold():
    """After CB_THRESHOLD failures the provider is marked as tripped."""
    import proxy as p

    p._provider_cb = {}
    for _ in range(p._CB_THRESHOLD):
        await p._record_provider_failure("openrouter")

    assert p._is_provider_tripped("openrouter")


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success():
    """A successful response resets the consecutive failure counter."""
    import proxy as p

    p._provider_cb = {"openrouter": {"consecutive": 3, "tripped_until": 0.0}}
    await p._record_provider_success("openrouter")
    assert p._provider_cb["openrouter"]["consecutive"] == 0


# ── Metrics counters ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_incremented_on_success():
    import proxy as p

    await p._m_record_request("openrouter", 200, 142.0)
    assert p._m_requests_total[("openrouter", "200")] == 1
    assert p._m_duration_sum["openrouter"] == 142.0
    assert p._m_duration_count["openrouter"] == 1


@pytest.mark.asyncio
async def test_metrics_429_counter():
    import proxy as p

    await p._m_record_429("openrouter", "sk-or-v1-abc...xyz", "model-a")
    assert p._m_429_total[("openrouter", "sk-or-v1-abc...xyz", "model-a")] == 1


# ── Key hint helper ───────────────────────────────────────────────────────────

def test_key_hint_short_key():
    import proxy as p
    assert p._key_hint("short") == "short"


def test_key_hint_long_key():
    import proxy as p
    key = "sk-or-v1-f6d1d44b229d9a265c599215e050d6c7"
    hint = p._key_hint(key)
    # _key_hint returns first 12 chars + "..." + last 4 chars
    assert hint == f"{key[:12]}...{key[-4:]}"
    assert "..." in hint
    assert len(hint) == 12 + 3 + 4
