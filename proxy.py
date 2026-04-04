"""
RotaKey v6 — Lightweight API Key + Model Fallback Proxy
========================================================
Changes from v5:

  NEW 1 — Per-key, per-model rate-limit tracking.
           429s now record which (provider, key, model) combo is cooled,
           not just which key. The proxy tries the next model in the
           provider's fallback chain before rotating to the next key.

  NEW 2 — Smart 429 dispatch order:
           (a) Try next model in provider's chain (same key).
           (b) If all models exhausted on this key → rotate to next key.
           (c) If all keys exhausted on this provider → spill to next provider.
           (d) If all providers exhausted → return 429 to client immediately.

  NEW 3 — Provider spillover on 429.
           Providers are tried in the order they appear in rotakey.yaml.
           On spill, model name is rewritten via the global model_map.
           If no mapping exists for the target provider, it is skipped.

  NEW 4 — Per-provider model_fallback chains.
           model_fallback moved from a single global block into each
           provider's config. Each provider has its own chain and
           trigger_statuses list.

  NEW 5 — Rate-limit state persisted to rotakey_state.json.
           Loaded on startup so cooled-down combos survive restarts.
           Expired entries are treated as available automatically.

  NEW 6 — /status endpoint shows per-provider, per-key cooldown state.
           Key hint = first 12 chars + "..." + last 4 chars.
           Key status: active / degraded / dead.

  NEW 7 — Cooldown timer: uses Retry-After header if present,
           else falls back to backoff_schedule[hit_count].

Retained from v5:
  - Colored console logs (GREY/GREEN/YELLOW/RED/CYAN/MAGENTA)
  - Retry-After header parsing
  - hit_count decay via RECOVERY_WINDOW
  - Structured 429 error JSON with retry_after
  - Per-request ID for log correlation
  - Rate-limit header logging
  - Upstream request-id / cf-ray logging
  - Rotating file log (2 MB x 5)
  - Atomic config reload with mtime check
  - Single AsyncClient created in lifespan
  - 16 MB body size cap
  - Auto port selection
  - SSE streaming with client disconnect detection
"""

import asyncio
import itertools
import json
import logging
import logging.handlers
import os
import re
import random
import socket
import stat
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
import uvicorn


# ── Paths ─────────────────────────────────────────────────────────────────────

_BASE_DIR   = Path(__file__).parent
CONFIG_FILE = _BASE_DIR / "rotakey.yaml"

MAX_BODY_BYTES = 16 * 1024 * 1024   # 16 MB hard cap


# ── Color support ─────────────────────────────────────────────────────────────

class _C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREY    = "\033[90m"
    WHITE   = "\033[97m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"
    ORANGE  = "\033[33m"


def _supports_color() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
            return True
        except Exception:
            return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _supports_color()


# ── Logging ───────────────────────────────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    LEVEL_STYLES = {
        logging.DEBUG:    (_C.GREY,                "[DBG]"),
        logging.INFO:     (_C.GREEN,               "[INF]"),
        logging.WARNING:  (_C.YELLOW,              "[WRN]"),
        logging.ERROR:    (_C.RED,                 "[ERR]"),
        logging.CRITICAL: (_C.RED + _C.BOLD,       "[CRT]"),
    }

    # Maps a keyword in the message to (prefix_tag, color)
    # Checked in order — first match wins.
    _MSG_STYLES: list[tuple[str, str, str]] = [
        # Incoming request
        ("──►",              "→ REQ   ",  _C.CYAN),
        # Clean success
        ("✓ 200",            "✓ OK    ",  _C.GREEN),
        # Model fallback triggered
        ("⤵ FALLBACK",       "⤵ FBACK ",  _C.MAGENTA),
        # Provider spill triggered
        ("⤳ SPILL",          "⤳ SPILL ",  _C.BLUE),
        # 429 rate-limit hit
        ("✗ 429",            "✗ 429   ",  _C.YELLOW),
        # Key dead (401/403)
        ("✗ KEY-DEAD",       "✗ DEAD  ",  _C.RED),
        # Key rotation — moving to next key
        ("↻ KEY-ROTATE",     "↻ ROTATE",  _C.ORANGE),
        # All keys on provider exhausted
        ("⚠ EXHAUSTED",      "⚠ EXHAUST", _C.RED + _C.BOLD),
        # Model skip (all keys cooled for this model)
        ("↷ MODEL-SKIP",     "↷ SKIP  ",  _C.YELLOW),
        # Timeout
        ("TIMEOUT",          "⏱ TIMED ",  _C.YELLOW),
        # Stream events
        ("streaming SSE",    "~ STREAM",  _C.CYAN),
        ("CLIENT DISCONNECTED", "✂ ABORT ", _C.ORANGE),
        # Cooldown recorded
        ("cooldown=",        "❄ COOL  ",  _C.BLUE),
        # Success clear
        ("cooldown cleared", "✓ RESUME",  _C.GREEN),
    ]

    def format(self, record: logging.LogRecord) -> str:
        color, label = self.LEVEL_STYLES.get(record.levelno, (_C.RESET, "[???]"))
        ts  = self.formatTime(record, self.datefmt)
        msg = record.getMessage()

        if _USE_COLOR:
            event_tag   = ""
            event_color = color
            for keyword, tag, ecol in self._MSG_STYLES:
                if keyword in msg:
                    event_tag   = tag
                    event_color = ecol
                    break

            # Dim the req_id bracket for readability
            msg = re.sub(r"(\[#\d+\])", f"{_C.DIM}\\1{_C.RESET}{event_color}", msg, count=1)

            ts_str  = f"{_C.GREY}{ts}{_C.RESET}"
            lbl_str = f"{color}{label}{_C.RESET}"
            tag_str = (
                f" {_C.BOLD}{event_color}{event_tag}{_C.RESET}" if event_tag else ""
            )
            msg_str = f"{event_color}{msg}{_C.RESET}"
            return f"{ts_str} {lbl_str}{tag_str} {msg_str}"

        return f"{ts} {label} {msg}"


log = logging.getLogger("rotakey")
log.setLevel(logging.DEBUG)


# ── JSON structured log formatter (for log_format: json in config) ────────────

class _JSONFormatter(logging.Formatter):
    """Emits one JSON object per line — compatible with Datadog, Loki, CloudWatch."""

    # Strip ANSI color codes that the console formatter may have already embedded
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def format(self, record: logging.LogRecord) -> str:
        msg = self._ANSI_RE.sub("", record.getMessage())
        obj = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "message": msg,
        }
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)

try:
    _stdout_utf8 = (
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
        if getattr(sys.stdout, "encoding", "utf-8").lower().replace("-", "") != "utf8"
        else sys.stdout
    )
except Exception:
    _stdout_utf8 = sys.stdout

_ch = logging.StreamHandler(stream=_stdout_utf8)
_ch.setLevel(logging.DEBUG)
_ch.setFormatter(_ColorFormatter(datefmt="%H:%M:%S"))
log.addHandler(_ch)

# req_id includes a startup-epoch prefix so IDs never collide across restarts.
# Format:  "<epoch_minute>-<counter>"  e.g. "29187342-1", "29187342-2", …
_req_epoch   = int(time.time()) // 60   # minutes since Unix epoch — compact but unique per restart
_req_counter = itertools.count(1)

def _next_req_id() -> str:
    return f"{_req_epoch}-{next(_req_counter)}"


# ── Config loader ─────────────────────────────────────────────────────────────

_cfg:            dict  = {}
_cfg_mtime:      float = 0.0
_cfg_last_check: float = 0.0
_cfg_lock = asyncio.Lock()


def _load_yaml_from_disk() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config file not found: {CONFIG_FILE}")
        log.error("Create rotakey.yaml next to proxy.py.")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _inject_env_keys(data)
    return data


def _inject_env_keys(cfg: dict) -> None:
    """Merge API keys from environment variables into the config dict.

    Supports two patterns (both may be combined):

    1. Comma-separated list (all keys for a provider in one var):
         ROTAKEY_KEYS_OPENROUTER=sk-or-v1-aaa,sk-or-v1-bbb
         ROTAKEY_KEYS_ANTHROPIC=sk-ant-xxx

    2. Indexed individual keys (useful for secrets managers that store one
       secret per entry):
         ROTAKEY_KEY_OPENROUTER_1=sk-or-v1-aaa
         ROTAKEY_KEY_OPENROUTER_2=sk-or-v1-bbb
         ROTAKEY_KEY_ANTHROPIC_1=sk-ant-xxx

    Keys injected from env are de-duplicated against existing YAML keys and
    against each other.  The YAML keys come first so the preferred-key
    ordering is preserved.
    """
    keys_section = cfg.setdefault("keys", {})
    providers    = list(cfg.get("providers", {}).keys())

    for provider in providers:
        existing = list(keys_section.get(provider) or [])
        seen     = set(existing)
        added    = []

        # Pattern 1: ROTAKEY_KEYS_<PROVIDER>=key1,key2,...
        bulk_var = f"ROTAKEY_KEYS_{provider.upper()}"
        bulk_val = os.environ.get(bulk_var, "").strip()
        if bulk_val:
            for k in (k.strip() for k in bulk_val.split(",") if k.strip()):
                if k not in seen:
                    added.append(k)
                    seen.add(k)

        # Pattern 2: ROTAKEY_KEY_<PROVIDER>_<N>=key  (N=1..99)
        for n in range(1, 100):
            idx_var = f"ROTAKEY_KEY_{provider.upper()}_{n}"
            idx_val = os.environ.get(idx_var, "").strip()
            if not idx_val:
                break          # Stop at first gap (e.g. _1 missing → stop)
            if idx_val not in seen:
                added.append(idx_val)
                seen.add(idx_val)

        if added:
            log.debug(
                f"  [{provider}] injected {len(added)} key(s) from environment"
            )
            keys_section[provider] = existing + added


def _attach_file_log(cfg: dict) -> None:
    srv      = cfg.get("server", {})
    log_file = _BASE_DIR / srv.get("log_file", "rotakey.log")
    use_json = srv.get("log_format", "text").lower() == "json"

    fmt = _JSONFormatter() if use_json else logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    for h in log.handlers[:]:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            log.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)


def _cfg_sync() -> dict:
    """Synchronous load used before the event loop starts."""
    global _cfg, _cfg_mtime, _cfg_last_check
    _cfg            = _load_yaml_from_disk()
    _cfg_mtime      = CONFIG_FILE.stat().st_mtime
    _cfg_last_check = time.monotonic()
    _attach_file_log(_cfg)
    return _cfg


async def get_cfg(*, force: bool = False) -> dict:
    """Atomic async config reload — only re-reads disk when mtime changes."""
    global _cfg, _cfg_mtime, _cfg_last_check

    now = time.monotonic()
    ttl = (_cfg or {}).get("rate_limit", {}).get("keys_cache_ttl", 30)

    if not force and _cfg and (now - _cfg_last_check) < ttl:
        return _cfg

    async with _cfg_lock:
        now = time.monotonic()
        if not force and _cfg and (now - _cfg_last_check) < ttl:
            return _cfg

        try:
            mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            log.error("Cannot stat rotakey.yaml — using cached config")
            return _cfg

        if mtime != _cfg_mtime or not _cfg:
            first_load = not _cfg
            _cfg       = _load_yaml_from_disk()
            _cfg_mtime = mtime
            if first_load:
                _attach_file_log(_cfg)
                log.info("Config loaded from rotakey.yaml")
            else:
                log.debug("Config hot-reloaded from rotakey.yaml (file changed)")

        _cfg_last_check = now
        return _cfg


# ── Security check ────────────────────────────────────────────────────────────

def _check_config_permissions() -> None:
    if os.name == "nt":
        log.warning("SECURITY: rotakey.yaml is unencrypted. Keep this folder private.")
        return
    mode = CONFIG_FILE.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("SECURITY: rotakey.yaml is readable by other users. Fixing...")
        CONFIG_FILE.chmod(0o600)
        log.info("Fixed: rotakey.yaml is now chmod 600 (owner read/write only).")


# ── Provider helpers ──────────────────────────────────────────────────────────

def _providers(cfg: dict) -> dict:
    return cfg.get("providers", {})


def _detect_provider(cfg: dict, path: str, query_provider: str) -> tuple[str, str]:
    providers = _providers(cfg)
    if query_provider in providers:
        return query_provider, path
    for name, pdef in providers.items():
        prefix = pdef.get("prefix", f"/{name}")
        if path.startswith(prefix):
            return name, path[len(prefix):]
    first = next(iter(providers), None)
    return (first, path) if first else ("openrouter", path)


# ── Namespace routing ─────────────────────────────────────────────────────────
# When a model ID is prefixed with  rotakey/{provider}/  the proxy routes
# directly to that provider without needing any URL prefix or model_patterns.
#
# Examples:
#   "rotakey/openrouter/minimax/minimax-m2.5:free"
#       → provider=openrouter   forwarded model="minimax/minimax-m2.5:free"
#   "rotakey/anthropic/claude-haiku-4-5-20251001"
#       → provider=anthropic    forwarded model="claude-haiku-4-5-20251001"
#   "rotakey/openrouter/claude-3-haiku"
#       → provider=openrouter   forwarded model="claude-3-haiku"
#         (same model, different route — solves the ambiguity problem)
#
# If no "rotakey/" prefix is present the existing detection logic runs
# unchanged (URL prefix → model_patterns → yaml-order fallback).

_ROTAKEY_NS = "rotakey/"


def _parse_model_ns(model: str) -> tuple[str | None, str]:
    """Split  rotakey/{provider}/{real_model}  into (provider, real_model).

    Returns (None, original_model) when no rotakey/ prefix is present so
    callers can treat both cases uniformly.

    >>> _parse_model_ns("rotakey/openrouter/minimax/minimax-m2.5:free")
    ("openrouter", "minimax/minimax-m2.5:free")
    >>> _parse_model_ns("minimax/minimax-m2.5:free")
    (None, "minimax/minimax-m2.5:free")
    """
    if not model.startswith(_ROTAKEY_NS):
        return None, model
    rest = model[len(_ROTAKEY_NS):]          # "openrouter/minimax/minimax-m2.5:free"
    try:
        slash = rest.index("/")
        return rest[:slash], rest[slash + 1:]  # ("openrouter", "minimax/minimax-m2.5:free")
    except ValueError:
        # "rotakey/openrouter" with nothing after — no real model, ignore prefix
        return None, model


def _rewrite_model(body: bytes, new_model: str) -> bytes:
    try:
        data          = json.loads(body)
        data["model"] = new_model
        return json.dumps(data).encode()
    except Exception:
        return body


def _key_hint(key: str) -> str:
    """first 12 chars + '...' + last 4 chars"""
    if len(key) <= 16:
        return key
    return f"{key[:12]}...{key[-4:]}"


def _translate_model(cfg: dict, model: str, target_provider: str) -> str | None:
    """
    Look up model in global model_map for target_provider.
    Returns translated name, or None if no mapping exists (skip provider).
    """
    model_map = cfg.get("model_map") or {}
    if not model_map:
        # No map at all — pass model name through as-is
        return model
    entry = model_map.get(model)
    if entry is None:
        # Model not in map at all — pass through as-is
        return model
    translated = entry.get(target_provider)
    if translated is None:
        # Model is in map but has no entry for this provider — skip provider
        return None
    return translated


# ── Per-key, per-model rate-limit state ───────────────────────────────────────
#
# State shape (in-memory mirror of rotakey_state.json):
# {
#   "openrouter": {
#     "sk-or-v1-abc...": {
#       "minimax/minimax-m2.5:free": {"cooled_until": 1234567890.0, "hit_count": 2},
#       ...
#     }
#   }
# }
#
# Separate dict for whole-key cooldowns (401/403 invalid key):
# {
#   "openrouter": {
#     "sk-or-v1-abc...": {"cooled_until": 1234567890.0}
#   }
# }

_rl_state:      dict[str, dict[str, dict[str, dict]]] = {}   # provider→key→model→{cooled_until, hit_count}
_key_dead:      dict[str, dict[str, float]]           = {}   # provider→key→cooled_until (401/403)
_last_ok_key:   dict[str, str]                        = {}   # provider→key that last returned 200
_rl_state_lock  = asyncio.Lock()

# ── Per-provider circuit breaker ──────────────────────────────────────────────
# Trips after CB_THRESHOLD consecutive timeouts or 5xx errors.
# Provider is skipped for CB_TRIP_SECS seconds, then re-probed automatically.
_CB_THRESHOLD  = 5
_CB_TRIP_SECS  = 60
_provider_cb:      dict[str, dict] = {}   # provider → {consecutive: int, tripped_until: float}
_provider_cb_lock  = asyncio.Lock()


# ── Lightweight Prometheus-compatible metrics ─────────────────────────────────
# No external dependency — emits standard text exposition format on /metrics.
#
#   rotakey_requests_total{provider, status}          counter
#   rotakey_429_total{provider, key_hint, model}      counter
#   rotakey_errors_total{provider, error_type}        counter
#   rotakey_request_duration_ms_sum{provider}         counter (sum of latencies)
#   rotakey_request_duration_ms_count{provider}       counter
#   rotakey_keys_active{provider}                     gauge   (updated on /metrics)
#   rotakey_keys_total{provider}                      gauge

_m_requests_total:  dict = defaultdict(int)   # (provider, status) -> count
_m_429_total:       dict = defaultdict(int)   # (provider, key_hint, model) -> count
_m_errors_total:    dict = defaultdict(int)   # (provider, error_type) -> count
_m_duration_sum:    dict = defaultdict(float) # provider -> ms sum
_m_duration_count:  dict = defaultdict(int)   # provider -> count
_m_lock = asyncio.Lock()


async def _m_record_request(provider: str, status: int, duration_ms: float) -> None:
    async with _m_lock:
        _m_requests_total[(provider, str(status))] += 1
        _m_duration_sum[provider]   += duration_ms
        _m_duration_count[provider] += 1


async def _m_record_429(provider: str, key_hint: str, model: str) -> None:
    async with _m_lock:
        _m_429_total[(provider, key_hint, model)] += 1


async def _m_record_error(provider: str, error_type: str) -> None:
    async with _m_lock:
        _m_errors_total[(provider, error_type)] += 1


def _m_render_text(cfg: dict) -> str:
    """Render all metrics as Prometheus text exposition format."""
    lines: list[str] = []

    def _push(name: str, help_: str, type_: str, samples: list[tuple]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {type_}")
        for labels, val in samples:
            lstr = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{lstr}}} {val}")

    # requests_total
    _push(
        "rotakey_requests_total",
        "Total proxied requests by provider and HTTP status",
        "counter",
        [
            ({"provider": prov, "status": st}, count)
            for (prov, st), count in sorted(_m_requests_total.items())
        ],
    )

    # 429_total
    _push(
        "rotakey_429_total",
        "Total 429 rate-limit hits by provider, key hint, and model",
        "counter",
        [
            ({"provider": p, "key_hint": kh, "model": m}, count)
            for (p, kh, m), count in sorted(_m_429_total.items())
        ],
    )

    # errors_total
    _push(
        "rotakey_errors_total",
        "Total proxy errors by provider and error type",
        "counter",
        [
            ({"provider": p, "error_type": et}, count)
            for (p, et), count in sorted(_m_errors_total.items())
        ],
    )

    # duration sum/count
    _push(
        "rotakey_request_duration_ms_sum",
        "Sum of upstream request durations in milliseconds",
        "counter",
        [({"provider": p}, round(v, 2)) for p, v in sorted(_m_duration_sum.items())],
    )
    _push(
        "rotakey_request_duration_ms_count",
        "Number of upstream requests timed",
        "counter",
        [({"provider": p}, v) for p, v in sorted(_m_duration_count.items())],
    )

    # keys gauge — computed live
    all_keys = cfg.get("keys", {})
    now      = time.time()
    for pname, pkeys in all_keys.items():
        if not pkeys:
            continue
        active = sum(1 for k in pkeys if not _is_key_dead(pname, k))
        _push(
            "rotakey_keys_active",
            "Number of non-dead API keys per provider",
            "gauge",
            [({"provider": pname}, active)],
        )
        _push(
            "rotakey_keys_total",
            "Total API keys configured per provider",
            "gauge",
            [({"provider": pname}, len(pkeys))],
        )

    lines.append("")
    return "\n".join(lines)


def _is_provider_tripped(provider: str) -> bool:
    cb = _provider_cb.get(provider, {})
    return cb.get("tripped_until", 0.0) > time.time()


async def _record_provider_failure(provider: str) -> None:
    """Increment failure counter; trip the breaker when threshold is reached."""
    async with _provider_cb_lock:
        cb = _provider_cb.setdefault(
            provider, {"consecutive": 0, "tripped_until": 0.0}
        )
        cb["consecutive"] += 1
        if cb["consecutive"] >= _CB_THRESHOLD and cb["tripped_until"] < time.time():
            cb["tripped_until"] = time.time() + _CB_TRIP_SECS
            log.error(
                f"⚡ CIRCUIT BREAKER [{provider.upper()}] tripped after "
                f"{cb['consecutive']} consecutive failures — "
                f"skipping provider for {_CB_TRIP_SECS}s"
            )


async def _record_provider_success(provider: str) -> None:
    """Reset failure counter after a clean response."""
    async with _provider_cb_lock:
        if provider in _provider_cb:
            _provider_cb[provider]["consecutive"]  = 0
            _provider_cb[provider]["tripped_until"] = 0.0


def _state_file(cfg: dict) -> Path:
    fname = cfg.get("rate_limit", {}).get("state_file", "rotakey_state.json")
    return _BASE_DIR / fname


def _load_state(cfg: dict) -> None:
    """Load persisted rate-limit state from disk on startup."""
    global _rl_state, _key_dead, _last_ok_key
    sf = _state_file(cfg)
    if not sf.exists():
        log.debug(f"No state file found at {sf} — starting fresh")
        return
    try:
        with open(sf, encoding="utf-8") as f:
            raw = json.load(f)
        _rl_state    = raw.get("model_cooldowns", {})
        _key_dead    = raw.get("key_dead", {})
        _last_ok_key = raw.get("last_ok_key", {})
        now = time.time()
        # Count how many entries are still active vs already expired
        active = sum(
            1
            for pdata in _rl_state.values()
            for kdata in pdata.values()
            for mdata in kdata.values()
            if mdata.get("cooled_until", 0) > now
        )
        log.info(f"State loaded from {sf.name} — {active} active cooldown(s)")
    except Exception as e:
        log.warning(f"Could not load state file {sf}: {e} — starting fresh")
        _rl_state    = {}
        _key_dead    = {}
        _last_ok_key = {}


def _save_state(cfg: dict) -> None:
    """Persist rate-limit state to disk.

    IMPORTANT: call this OUTSIDE _rl_state_lock.  Build a snapshot while
    holding the lock, release the lock, then call this function.  Synchronous
    file I/O inside an asyncio lock blocks the entire event loop.
    """
    sf = _state_file(cfg)
    try:
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model_cooldowns": _rl_state,
                    "key_dead":        _key_dead,
                    "last_ok_key":     _last_ok_key,
                },
                f,
                indent=2,
            )
        if os.name != "nt":
            sf.chmod(0o600)
    except Exception as e:
        log.warning(f"Could not save state file {sf}: {e}")


def _model_cooled_until(provider: str, key: str, model: str) -> float:
    """Return the cooled_until timestamp for (provider, key, model), or 0.0."""
    return (
        _rl_state
        .get(provider, {})
        .get(key, {})
        .get(model, {})
        .get("cooled_until", 0.0)
    )


def _is_model_available(provider: str, key: str, model: str) -> bool:
    return _model_cooled_until(provider, key, model) < time.time()


def _is_key_dead(provider: str, key: str) -> bool:
    return _key_dead.get(provider, {}).get(key, 0.0) > time.time()


def _available_models_for_key(provider: str, key: str, chain: list[str]) -> list[str]:
    """Return models from chain that are not currently cooled for this key."""
    return [m for m in chain if _is_model_available(provider, key, m)]


def _all_models_cooled_for_key(provider: str, key: str, chain: list[str]) -> bool:
    return all(not _is_model_available(provider, key, m) for m in chain)


def _ordered_keys(provider: str, keys: list[str]) -> list[str]:
    """
    Return keys in priority order:
      1. Last key that returned 200 for this provider (if still alive)
      2. All other non-dead keys in original list order
    This gives request-to-request key affinity without hard locking.
    """
    preferred = _last_ok_key.get(provider)
    if preferred and preferred in keys and not _is_key_dead(provider, preferred):
        rest = [k for k in keys if k != preferred]
        return [preferred] + rest
    return list(keys)


def _fmt_expiry(ts: float) -> str:
    secs = max(0, round(ts - time.time()))
    return f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')} ({secs}s from now)"


async def mark_model_cooldown(
    provider: str,
    key: str,
    model: str,
    cfg: dict,
    retry_after: int | None = None,
) -> None:
    """Record a 429 against (provider, key, model) and persist to disk."""
    rl       = cfg.get("rate_limit", {})
    schedule = rl.get("backoff_schedule", [60, 120, 300, 600])
    rec_win  = rl.get("recovery_window", 300)

    async with _rl_state_lock:
        now = time.time()
        pdata = _rl_state.setdefault(provider, {})
        kdata = pdata.setdefault(key, {})
        mdata = kdata.setdefault(model, {"cooled_until": 0.0, "hit_count": 0})

        last_hit = mdata.get("cooled_until", 0.0) - schedule[
            min(mdata.get("hit_count", 1) - 1, len(schedule) - 1)
        ] if mdata.get("hit_count", 0) > 0 else 0.0

        hits = mdata.get("hit_count", 0)
        # Reset hit count if the model has been clean long enough
        if hits > 0 and (now - (mdata["cooled_until"] - schedule[min(hits - 1, len(schedule) - 1)])) > rec_win:
            log.debug(
                f"  [{provider}] {_key_hint(key)} model={model}: "
                f"hit_count reset (recovery window passed)"
            )
            hits = 0

        if retry_after is not None:
            wait, src = retry_after, "Retry-After header"
        else:
            wait, src = schedule[min(hits, len(schedule) - 1)], "backoff_schedule"

        mdata["cooled_until"] = now + wait
        mdata["hit_count"]    = hits + 1
        kdata[model]          = mdata

        log.warning(
            f"  [{provider}] {_key_hint(key)} model='{model}' "
            f"cooldown={wait}s [{src}]  hit=#{hits + 1}  "
            f"recovers={_fmt_expiry(mdata['cooled_until'])}"
        )
    # ── disk write OUTSIDE the lock so we don't stall the event loop ──────────
    _save_state(cfg)


async def mark_key_dead(provider: str, key: str, status: int, cfg: dict) -> None:
    """Mark a key as dead (401/403) for invalid_key_cooldown seconds."""
    cd = cfg.get("rate_limit", {}).get("invalid_key_cooldown", 86_400)
    async with _rl_state_lock:
        _key_dead.setdefault(provider, {})[key] = time.time() + cd
        log.error(
            f"  [{provider}] {_key_hint(key)} rejected ({status}) "
            f"→ disabled for {cd // 3600}h"
        )
    # ── disk write OUTSIDE the lock ────────────────────────────────────────────
    _save_state(cfg)


async def clear_model_cooldown(provider: str, key: str, model: str, cfg: dict) -> None:
    """Record successful key, and clear any cooldown that was set for this model."""
    _need_save = False
    async with _rl_state_lock:
        # Always record the key that just worked so next request prefers it
        _last_ok_key[provider] = key
        mdata = _rl_state.get(provider, {}).get(key, {}).get(model)
        if mdata and mdata.get("cooled_until", 0) > 0:
            mdata["cooled_until"] = 0.0
            log.debug(f"  [{provider}] {_key_hint(key)} model='{model}': cooldown cleared on success")
            _need_save = True
        else:
            # _last_ok_key changed — persist that too
            _need_save = True
    # ── disk write OUTSIDE the lock ────────────────────────────────────────────
    if _need_save:
        _save_state(cfg)


# ── HTTP client — created once in lifespan ────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialised — lifespan bug")
    return _http_client


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Exactly one AsyncClient created at startup, closed at shutdown."""
    global _http_client
    cfg    = await get_cfg(force=True)
    _load_state(cfg)
    hc     = cfg.get("http_client", {})
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=hc.get("timeout_connect", 10.0),
            read=hc.get("timeout_read",       120.0),
            write=hc.get("timeout_write",      30.0),
            pool=hc.get("timeout_pool",         5.0),
        ),
        limits=httpx.Limits(
            max_connections=hc.get("max_connections", 20),
            max_keepalive_connections=hc.get("max_keepalive", 10),
        ),
        follow_redirects=False,
    )
    log.info(
        f"HTTP client ready  "
        f"(max_conn={hc.get('max_connections', 20)}  "
        f"keepalive={hc.get('max_keepalive', 10)}  "
        f"read_timeout={hc.get('timeout_read', 120.0)}s)"
    )
    yield
    await _http_client.aclose()
    log.info("HTTP client closed")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="RotaKey Proxy v6",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)

# ── Inbound authentication ────────────────────────────────────────────────────
# Set ROTAKEY_TOKEN env var to require all clients to send:
#   Authorization: Bearer <token>
# /health is always public (used by monitors that have no token).
# /status and all proxy routes are protected when the token is set.

_ROTAKEY_TOKEN: str = os.environ.get("ROTAKEY_TOKEN", "").strip()
if _ROTAKEY_TOKEN:
    log.info("Inbound auth ENABLED — clients must supply Authorization: Bearer <token>")
else:
    log.warning(
        "Inbound auth DISABLED (ROTAKEY_TOKEN not set). "
        "Any local process can use this proxy and drain your quota."
    )


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    # /health is always public — monitors need it without credentials
    if not _ROTAKEY_TOKEN or request.url.path in ("/health",):
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {_ROTAKEY_TOKEN}":
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Unauthorized — set Authorization: Bearer <ROTAKEY_TOKEN>",
                    "type":    "authentication_error",
                }
            }),
            status_code=401,
            media_type="application/json",
        )
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status_endpoint():
    cfg       = await get_cfg()
    providers = _providers(cfg)
    all_keys  = cfg.get("keys", {})
    now       = time.time()
    result    = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "providers": {}}

    async with _rl_state_lock:
        for rank, (pname, pdef) in enumerate(providers.items(), start=1):
            keys      = all_keys.get(pname, [])
            key_list  = []
            chain     = pdef.get("model_fallback", {}).get("chain", [])

            for key in keys:
                hint      = _key_hint(key)
                kdata     = _rl_state.get(pname, {}).get(key, {})
                dead_until = _key_dead.get(pname, {}).get(key, 0.0)

                if dead_until > now:
                    # 401/403 dead key
                    key_list.append({
                        "key_hint": hint,
                        "status": "dead_invalid",
                        "dead_until": datetime.fromtimestamp(dead_until).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "cooled_models": [],
                    })
                    continue

                cooled_models = []
                for model, mdata in kdata.items():
                    cu = mdata.get("cooled_until", 0.0)
                    if cu > now:
                        cooled_models.append({
                            "model":     model,
                            "until":     datetime.fromtimestamp(cu).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "until_secs": max(0, round(cu - now)),
                            "hit_count": mdata.get("hit_count", 0),
                        })

                # Determine key status
                if not cooled_models:
                    key_status = "active"
                elif chain and all(
                    kdata.get(m, {}).get("cooled_until", 0.0) > now for m in chain
                ):
                    key_status = "dead"   # all chain models cooled = effectively dead
                else:
                    key_status = "degraded"

                key_list.append({
                    "key_hint":     hint,
                    "status":       key_status,
                    "cooled_models": sorted(cooled_models, key=lambda x: -x["until_secs"]),
                })

            result["providers"][pname] = {
                "spillover_rank": rank,
                "keys_total":     len(keys),
                "keys_active":    sum(1 for k in key_list if k["status"] == "active"),
                "keys_degraded":  sum(1 for k in key_list if k["status"] == "degraded"),
                "keys_dead":      sum(1 for k in key_list if k["status"] in ("dead", "dead_invalid")),
                "fallback_chain": chain,
                "keys":           key_list,
            }

    return result


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus-compatible text exposition format.

    Scrape with:  prometheus.yml → scrape_configs → static_configs:
                    targets: ['localhost:8765']
                    metrics_path: '/metrics'
    """
    cfg = await get_cfg()
    return Response(
        content=_m_render_text(cfg),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ── Core proxy handler ────────────────────────────────────────────────────────

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    req_id  = _next_req_id()
    t_start = time.monotonic()
    cfg     = await get_cfg()

    # Body size cap
    cl = int(request.headers.get("content-length", 0) or 0)
    if cl > MAX_BODY_BYTES:
        return Response(
            content=json.dumps({"error": {"message": f"Request body too large (max {MAX_BODY_BYTES // 1_048_576} MB)", "type": "invalid_request_error"}}),
            status_code=413, media_type="application/json",
        )
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(
            content=json.dumps({"error": {"message": f"Request body too large (max {MAX_BODY_BYTES // 1_048_576} MB)", "type": "invalid_request_error"}}),
            status_code=413, media_type="application/json",
        )

    query_provider = request.query_params.get("provider", "")

    # ── Parse model first — namespace routing needs it before provider detect ──
    original_model = "unknown"
    try:
        original_model = json.loads(body).get("model", "unknown")
    except Exception:
        pass

    # ── Namespace routing: rotakey/{provider}/{real_model} ────────────────────
    # Strip the prefix from the body immediately so every downstream code path
    # (fallback chain, model rewrite, logging) sees the clean model name.
    ns_provider, clean_model = _parse_model_ns(original_model)
    if ns_provider:
        original_model = clean_model
        body           = _rewrite_model(body, original_model)

    all_keys  = cfg.get("keys", {})
    providers = _providers(cfg)

    if ns_provider and ns_provider not in providers:
        log.warning(
            f"[#{req_id}] namespace provider '{ns_provider}' not in config "
            f"— available: {list(providers.keys())} — falling back to detection"
        )
        ns_provider = None   # fall through to normal detection

    if ns_provider and not query_provider:
        # Namespace gave us an unambiguous provider — skip prefix detection.
        # Full request path is forwarded as-is (no URL prefix to strip).
        primary_provider = ns_provider
        stripped_path    = "/" + path
        log.debug(f"[#{req_id}] namespace-routed → provider={primary_provider}")
    else:
        primary_provider, stripped_path = _detect_provider(
            cfg, "/" + path, query_provider
        )

    # ── Strip bare provider prefix if client omitted the rotakey/ namespace ──
    # Handles: "openrouter/minimax/minimax-m2.5:free" → "minimax/minimax-m2.5:free"
    # This makes the proxy tolerant of clients that send  "{provider}/{model}"
    # instead of the canonical "rotakey/{provider}/{model}" form.
    if not ns_provider:
        bare_prefix = primary_provider + "/"
        if original_model.startswith(bare_prefix):
            clean_model    = original_model[len(bare_prefix):]
            log.debug(
                f"[#{req_id}] bare provider prefix stripped: "
                f"'{original_model}' → '{clean_model}'"
            )
            original_model = clean_model
            body           = _rewrite_model(body, original_model)

    log.info(
        f"[#{req_id}] ──► {request.method} /{path[:60]}"
        f"  model={original_model}  provider={primary_provider}"
        f"  body={len(body)}B"
    )

    # Build provider order: primary first, then rest in yaml order
    provider_order = [primary_provider] + [
        p for p in providers if p != primary_provider
    ]

    current_body  = body
    current_model = original_model

    for provider_idx, provider_name in enumerate(provider_order):
        pdef = providers.get(provider_name)
        if not pdef:
            continue
        keys = all_keys.get(provider_name, [])
        if not keys:
            log.debug(f"[#{req_id}] skipping {provider_name} — no keys configured")
            continue

        # ── Circuit breaker check ─────────────────────────────────────────────
        if _is_provider_tripped(provider_name):
            cb = _provider_cb.get(provider_name, {})
            secs_left = max(0, round(cb.get("tripped_until", 0) - time.time()))
            log.warning(
                f"[#{req_id}] ⚡ CIRCUIT BREAKER [{provider_name.upper()}] "
                f"provider is tripped — skipping (resets in {secs_left}s)"
            )
            continue

        # On provider spill, translate model name via model_map
        if provider_name != primary_provider:
            translated = _translate_model(cfg, original_model, provider_name)
            if translated is None:
                log.warning(
                    f"[#{req_id}] ⤳ SPILL → {provider_name}: "
                    f"no model_map entry for '{original_model}' → skipping provider"
                )
                continue
            if translated != current_model:
                log.warning(
                    f"[#{req_id}] ⤳ SPILL → {provider_name}: "
                    f"model rewritten '{original_model}' → '{translated}'"
                )
                current_model = translated
                current_body  = _rewrite_model(body, current_model)
            else:
                log.warning(
                    f"[#{req_id}] ⤳ SPILL → {provider_name}: "
                    f"model='{current_model}' (no rewrite needed)"
                )

        # ── Key + model loop ──────────────────────────────────────────────────
        # Architecture (per spec):
        #   OUTER: iterate keys in priority order (sticky/preferred key first)
        #   INNER: iterate models in chain for each key
        #
        # For each key K:
        #   Try each model M in chain that is not cooled on K.
        #   429  → mark (K,M) cooled, try next model on same K.
        #   All models on K cooled → rotate to next key K+1.
        #   All keys exhausted → spill to next provider.
        #
        # 5xx/404: model fallback only (no key rotation). Stay on same key,
        #          advance model, continue.

        # Per-provider fallback config (needed for fb_statuses)
        fb_cfg      = pdef.get("model_fallback", {})
        fb_chain    = fb_cfg.get("chain", [])
        fb_statuses = set(fb_cfg.get("trigger_statuses", [500, 502, 503, 404]))

        if provider_name != primary_provider:
            log.warning(
                f"[#{req_id}] ⤳ SPILL  {primary_provider} → {provider_name} "
                f"({len(keys)} keys  {len(fb_chain)} models in chain)"
            )

        # Build ordered key list: preferred (last-ok) key first
        async with _rl_state_lock:
            ordered_keys = _ordered_keys(provider_name, keys)

        for key in ordered_keys:
            hint = _key_hint(key)

            # Skip dead keys (401/403 cooldown)
            async with _rl_state_lock:
                if _is_key_dead(provider_name, key):
                    log.debug(f"[#{req_id}] skipping {hint} [{provider_name}] — key is dead")
                    continue

            # Build model chain starting from current_model for this provider
            if fb_chain:
                if current_model in fb_chain:
                    model_chain = fb_chain[fb_chain.index(current_model):]
                else:
                    model_chain = [current_model] + fb_chain
            else:
                model_chain = [current_model]

            # Filter to models not yet cooled on THIS key
            async with _rl_state_lock:
                available_models = [
                    m for m in model_chain
                    if _is_model_available(provider_name, key, m)
                ]

            if not available_models:
                log.warning(
                    f"[#{req_id}] ↷ MODEL-SKIP [{provider_name}] {hint} "
                    f"all {len(model_chain)} model(s) cooled on this key → rotating to next key"
                )
                continue   # all models on this key are cooled → try next key

            # ── Model loop for this key ───────────────────────────────────────
            model_idx = 0
            while model_idx < len(available_models):
                attempt_model = available_models[model_idx]

                forward_path = stripped_path.lstrip("/")
                target_url   = f"{pdef['base_url']}/{forward_path}"

                # Rewrite model in body if needed
                if attempt_model != current_model:
                    send_body = _rewrite_model(current_body, attempt_model)
                else:
                    send_body = current_body

                log.debug(
                    f"[#{req_id}] ↻ KEY-ROTATE [{provider_name}] {hint}  "
                    f"model='{attempt_model}'  url={target_url[:80]}"
                )

                headers = {
                    k: v for k, v in request.headers.items()
                    if k.lower() not in (
                        "host", "x-api-key", "authorization",
                        "content-length", "x-goog-api-key",
                    )
                }
                key_prefix                   = pdef.get("key_prefix", "")
                headers[pdef["key_header"]]  = f"{key_prefix}{key}"
                for h, v in pdef.get("extra_headers", {}).items():
                    headers[h] = v

                try:
                    t_req = time.monotonic()
                    _rq   = get_http_client().build_request(
                        method=request.method,
                        url=target_url,
                        headers=headers,
                        content=send_body,
                        params={k: v for k, v in request.query_params.items() if k != "provider"},
                    )
                    resp = await get_http_client().send(_rq, stream=True)
                    elapsed_ms = round((time.monotonic() - t_req) * 1000)
                    if "text/event-stream" not in resp.headers.get("content-type", ""):
                        await resp.aread()

                    upstream_id  = (
                        resp.headers.get("x-request-id") or
                        resp.headers.get("cf-ray") or ""
                    )
                    rl_remaining = resp.headers.get("x-ratelimit-remaining-requests", "")
                    rl_reset     = resp.headers.get("x-ratelimit-reset-requests", "")
                    rl_info      = (
                        f"  rl_remaining={rl_remaining} rl_reset={rl_reset}"
                        if (rl_remaining or rl_reset) else ""
                    )

                    # ── 429: mark (key, model) cooled → try next model on same key ──
                    if resp.status_code == 429:
                        retry_after: int | None = None
                        ra_raw = (
                            resp.headers.get("retry-after") or
                            resp.headers.get("x-ratelimit-reset-requests")
                        )
                        if ra_raw:
                            try:
                                retry_after = int(ra_raw)
                            except ValueError:
                                pass

                        err_hint = ""
                        try:
                            ej = resp.json()
                            err_hint = (
                                ej.get("error", {}).get("message", "") or
                                ej.get("message", "")
                            )[:160]
                        except Exception:
                            err_hint = resp.text[:160]

                        await mark_model_cooldown(
                            provider_name, key, attempt_model, cfg,
                            retry_after=retry_after,
                        )
                        await _m_record_429(provider_name, hint, attempt_model)

                        next_m = available_models[model_idx + 1] if model_idx + 1 < len(available_models) else None
                        if next_m:
                            next_action = f"trying next model on same key → '{next_m}'"
                        else:
                            next_action = f"all models cooled on {hint} → rotating to next key"

                        log.warning(
                            f"[#{req_id}] ✗ 429 [{provider_name}] {hint} "
                            f"model='{attempt_model}'  {elapsed_ms}ms"
                            + (f"  upstream_id={upstream_id}" if upstream_id else "")
                            + rl_info
                            + (f"\n           reason : {err_hint}" if err_hint else "")
                            + f"\n           action : {next_action}"
                        )
                        model_idx += 1   # try next model on this same key
                        continue

                    # ── 401/403: key dead — stop all models on this key ───────────
                    if resp.status_code in (401, 403):
                        await mark_key_dead(provider_name, key, resp.status_code, cfg)
                        async with _rl_state_lock:
                            live_keys = [
                                k for k in ordered_keys
                                if not _is_key_dead(provider_name, k) and k != key
                            ]
                        log.error(
                            f"[#{req_id}] ✗ KEY-DEAD [{provider_name}] {hint} "
                            f"HTTP {resp.status_code} — key rejected by upstream"
                            + (f"\n           action : rotating to next key ({len(live_keys)} remaining)" if live_keys
                               else f"\n           action : no live keys left on {provider_name}")
                        )
                        break   # break model loop → outer key loop continues to next key

                    # ── 5xx/404: model fallback only — stay on same key ───────────
                    if resp.status_code in fb_statuses and resp.status_code != 429:
                        err_hint = ""
                        try:
                            ej = resp.json()
                            err_hint = (
                                ej.get("error", {}).get("message", "") or
                                ej.get("message", "")
                            )[:200]
                        except Exception:
                            err_hint = resp.text[:200]

                        # Count toward circuit breaker — provider may be down
                        await _record_provider_failure(provider_name)

                        if model_idx + 1 < len(available_models):
                            next_model = available_models[model_idx + 1]
                            remaining  = len(available_models) - model_idx - 2
                            log.warning(
                                f"[#{req_id}] ⤵ FALLBACK [{provider_name}] {hint} "
                                f"HTTP {resp.status_code} — upstream error on model='{attempt_model}'"
                                f"\n           reason : {err_hint if err_hint else 'no detail from upstream'}"
                                f"\n           action : trying next model '{next_model}' (no key rotation)"
                                f"  ({remaining} more fallback(s) available)"
                            )
                            current_model = next_model
                            current_body  = _rewrite_model(current_body, current_model)
                            model_idx += 1
                            continue   # stay on same key, try next model
                        else:
                            log.error(
                                f"[#{req_id}] ⤵ FALLBACK [{provider_name}] {hint} "
                                f"HTTP {resp.status_code} — upstream error on model='{attempt_model}'"
                                f"\n           reason : {err_hint if err_hint else 'no detail from upstream'}"
                                f"\n           action : no more fallback models in chain — returning error"
                            )

                    # ── Success or terminal error ─────────────────────────────────
                    total_ms = round((time.monotonic() - t_start) * 1000)
                    await clear_model_cooldown(provider_name, key, attempt_model, cfg)
                    await _record_provider_success(provider_name)
                    await _m_record_request(provider_name, resp.status_code, elapsed_ms)

                    fallback_note = (
                        f"  [model: {original_model} → {attempt_model}]"
                        if attempt_model != original_model else ""
                    )
                    spill_note = (
                        f"  [spill: {primary_provider} → {provider_name}]"
                        if provider_name != primary_provider else ""
                    )

                    if resp.status_code >= 400:
                        err_detail = ""
                        try:
                            ej = resp.json()
                            err_detail = (
                                ej.get("error", {}).get("message", "") or
                                ej.get("message", "")
                            )[:200]
                        except Exception:
                            err_detail = resp.text[:200]
                        log.error(
                            f"[#{req_id}] HTTP {resp.status_code} [{provider_name}] {hint}"
                            f"  req={elapsed_ms}ms  total={total_ms}ms  model='{attempt_model}'"
                            + fallback_note + spill_note
                            + (f"  upstream_id={upstream_id}" if upstream_id else "")
                            + rl_info
                            + f"\n           reason : {err_detail}"
                        )
                    else:
                        content_len = resp.headers.get("content-length", "?")
                        log.info(
                            f"[#{req_id}] ✓ 200 [{provider_name}] {hint}"
                            f"  req={elapsed_ms}ms  total={total_ms}ms"
                            f"  model='{attempt_model}'  size={content_len}B"
                            + fallback_note + spill_note
                            + (f"  upstream_id={upstream_id}" if upstream_id else "")
                            + rl_info
                        )

                    content_type = resp.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        log.info(
                            f"[#{req_id}] streaming SSE [{provider_name}] {hint}"
                            f"  model='{attempt_model}'{fallback_note}{spill_note}"
                        )
                        _req_id = req_id
                        _hint   = hint
                        _model  = attempt_model

                        # ──────────────────────────────────────────────────────────────
                        # ROOT CAUSE OF CRASH (v6 patch 2):
                        #
                        # Python async generators CANNOT `await` inside a `finally`
                        # block when the runtime sends GeneratorExit into them.  If you
                        # do, Python raises:
                        #   RuntimeError: asynchronous generator ignored GeneratorExit
                        # This exception propagates up through uvicorn's ASGI handler
                        # and terminates the entire server process — which is exactly
                        # the "stream complete … [ERROR] Proxy failed to start" pattern
                        # we see in the log (clean stream, then immediate death).
                        #
                        # The GeneratorExit arrives when:
                        #   (a) The client disconnects mid-stream (uvicorn throws it in),
                        #   (b) The stream finishes normally — uvicorn still calls
                        #       aclose() on the generator, which sends GeneratorExit.
                        #
                        # SOLUTION: separate the cleanup (aclose + logging) completely
                        # from the generator.  The generator only yields bytes.
                        # Cleanup is scheduled as a Starlette Background task, which
                        # runs in a normal async function — no GeneratorExit risk there.
                        # ──────────────────────────────────────────────────────────────

                        # Shared state between generator and background task, passed
                        # by reference via a one-element list (simple mutable container).
                        _stream_state = {"chunks": 0, "has_data": False,
                                         "aborted": False, "t0": time.monotonic()}

                        async def _stream_gen(
                            response:       httpx.Response,
                            client_request: Request,
                            state:          dict,
                        ):
                            """Pure byte-yielding generator — NO awaits in finally."""
                            try:
                                async for chunk in response.aiter_bytes():
                                    if await client_request.is_disconnected():
                                        state["aborted"] = True
                                        return          # clean return, no yield after abort
                                    yield chunk
                                    state["chunks"] += 1
                                    if (not state["has_data"]
                                            and b"data:" in chunk
                                            and b"[DONE]" not in chunk):
                                        state["has_data"] = True
                            except GeneratorExit:
                                # Generator is being shut down by the runtime.
                                # We MUST NOT await here — just set the flag and return.
                                state["aborted"] = True
                                return
                            except Exception:
                                # Any other upstream read error — mark aborted so the
                                # background task logs it correctly, then propagate.
                                state["aborted"] = True
                                raise

                        async def _stream_cleanup(
                            response: httpx.Response,
                            rid:      str,
                            skey:     str,
                            model:    str,
                            state:    dict,
                        ):
                            """Runs after StreamingResponse finishes sending.
                            Safe to await here — this is a plain async function,
                            not a generator, so GeneratorExit is never injected."""
                            elapsed = round((time.monotonic() - state["t0"]) * 1000)
                            try:
                                await response.aclose()
                            except Exception:
                                pass
                            if state["aborted"]:
                                log.warning(
                                    f"[#{rid}] CLIENT DISCONNECTED (stop/abort) "
                                    f"model={model}  key={skey}  "
                                    f"chunks_sent={state['chunks']}  elapsed={elapsed}ms  "
                                    f"-> upstream closed, quota preserved"
                                )
                            elif not state["has_data"] and state["chunks"] > 0:
                                log.warning(
                                    f"[#{rid}] empty SSE stream  "
                                    f"model={model}  key={skey}  "
                                    f"chunks={state['chunks']}  elapsed={elapsed}ms  "
                                    f"-> no data lines received (keep-alive only); "
                                    f"stream closed cleanly, client may retry"
                                )
                            else:
                                log.debug(
                                    f"[#{rid}] stream complete  "
                                    f"chunks={state['chunks']}  elapsed={elapsed}ms"
                                )

                        return StreamingResponse(
                            _stream_gen(resp, request, _stream_state),
                            status_code=resp.status_code,
                            media_type="text/event-stream",
                            headers={
                                "Cache-Control":     "no-cache",
                                "X-Accel-Buffering": "no",
                            },
                            background=BackgroundTask(
                                _stream_cleanup,
                                resp, _req_id, _hint, _model, _stream_state,
                            ),
                        )

                    return Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        media_type=content_type,
                    )

                except httpx.TimeoutException:
                    elapsed_ms = round((time.monotonic() - t_req) * 1000)
                    log.warning(
                        f"[#{req_id}] TIMEOUT [{provider_name}] {hint}"
                        f"  {elapsed_ms}ms  model='{attempt_model}'"
                        f"\n           action : trying next model on same key"
                    )
                    await _record_provider_failure(provider_name)
                    await _m_record_error(provider_name, "timeout")
                    model_idx += 1
                except Exception as e:
                    elapsed_ms = round((time.monotonic() - t_req) * 1000)
                    log.error(
                        f"[#{req_id}] ERROR [{provider_name}] {hint}"
                        f"  {elapsed_ms}ms  {type(e).__name__}: {e}"
                    )
                    await _m_record_error(provider_name, type(e).__name__)
                    model_idx += 1

            # End of model loop for this key — continue to next key

        # All models exhausted for this provider
        has_next = any(
            all_keys.get(p) for p in provider_order[provider_idx + 1:]
        )
        log.error(
            f"[#{req_id}] ⚠ EXHAUSTED [{provider_name.upper()}] "
            f"all models + keys rate-limited (HTTP 429)"
            + (f"\n           action : ⤳ SPILL to next provider in chain" if has_next
               else "\n           action : no more providers — returning 429 to client")
        )

    # ── Every provider failed ─────────────────────────────────────────────────
    total_ms      = round((time.monotonic() - t_start) * 1000)
    all_keys_snap = cfg.get("keys", {})
    summaries     = []
    now           = time.time()

    for pname, pkeys in all_keys_snap.items():
        if not pkeys:
            continue
        pchain   = providers.get(pname, {}).get("model_fallback", {}).get("chain", [])
        n_dead   = sum(1 for k in pkeys if _is_key_dead(pname, k))
        n_cooled = sum(
            1 for k in pkeys
            if not _is_key_dead(pname, k) and pchain and _all_models_cooled_for_key(pname, k, pchain)
        )
        all_ts   = [
            _model_cooled_until(pname, k, m)
            for k in pkeys for m in pchain
            if _model_cooled_until(pname, k, m) > now
        ]
        retry_in = max(0, round(min(all_ts) - now)) if all_ts else 0
        summaries.append({
            "provider":                 pname,
            "keys_total":               len(pkeys),
            "keys_dead_invalid":        n_dead,
            "keys_all_models_cooled":   n_cooled,
            "soonest_retry_in_seconds": retry_in,
        })

    overall_retry = min(
        (p["soonest_retry_in_seconds"] for p in summaries if p["soonest_retry_in_seconds"] > 0),
        default=60,
    )

    log.error(
        f"[#{req_id}] ALL PROVIDERS FAILED  total={total_ms}ms  model={current_model}"
        f"  -> returning 429 (retry in {overall_retry}s)"
    )

    return Response(
        content=json.dumps({
            "error": {
                "message": (
                    f"RotaKey proxy: all models across all API keys across "
                    f"{len(summaries)} provider(s) are rate limited. "
                    f"Soonest recovery: ~{overall_retry}s. Please retry."
                ),
                "type":           "rate_limit_error",
                "code":           "all_keys_exhausted",
                "model":          current_model,
                "original_model": original_model,
                "retry_after":    overall_retry,
                "providers":      summaries,
                "rotakey_req_id": req_id,
            }
        }, indent=2).encode(),
        status_code=429,
        media_type="application/json",
        headers={"Retry-After": str(overall_retry)},
    )


# ── Port utilities ────────────────────────────────────────────────────────────

def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _find_free_port(start: int, limit: int = 20) -> int:
    for port in range(start, start + limit):
        if _is_port_free(port):
            return port
    raise RuntimeError(
        f"No free port in range {start}-{start + limit}. Set ROTAKEY_PORT manually."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # ── --validate: load config, deep schema check, print summary, exit ───────
    if "--validate" in sys.argv or "--check" in sys.argv:
        print("\n  RotaKey config validator\n  " + "─" * 40)
        try:
            v_cfg       = _load_yaml_from_disk()
            v_providers = v_cfg.get("providers", {})
            v_keys      = v_cfg.get("keys", {})
            v_rl        = v_cfg.get("rate_limit", {})
            v_srv       = v_cfg.get("server", {})

            errors: list[str] = []

            required_top = ["server", "rate_limit", "providers", "keys"]
            for k in required_top:
                if k not in v_cfg:
                    errors.append(f"Missing top-level key: '{k}'")

            # server section checks
            host = v_srv.get("host", "127.0.0.1")
            port = v_srv.get("port", 8765)
            if not isinstance(port, int) or not (1 <= port <= 65535):
                errors.append(f"server.port must be an integer 1-65535, got: {port!r}")
            if v_srv.get("log_format", "text") not in ("text", "json"):
                errors.append("server.log_format must be 'text' or 'json'")
            if v_srv.get("log_level", "INFO") not in ("DEBUG","INFO","WARNING","ERROR"):
                errors.append("server.log_level must be DEBUG|INFO|WARNING|ERROR")

            # rate_limit checks
            if not isinstance(v_rl.get("backoff_schedule", [60,120,300,600]), list):
                errors.append("rate_limit.backoff_schedule must be a list of integers")
            elif not all(isinstance(x, int) for x in v_rl.get("backoff_schedule", [])):
                errors.append("rate_limit.backoff_schedule entries must all be integers")
            for field in ("recovery_window", "invalid_key_cooldown", "keys_cache_ttl"):
                val = v_rl.get(field)
                if val is not None and not isinstance(val, (int, float)):
                    errors.append(f"rate_limit.{field} must be a number, got: {val!r}")

            # provider checks
            for pname, pdef in v_providers.items():
                if not pdef.get("base_url"):
                    errors.append(f"providers.{pname}: missing 'base_url'")
                if not pdef.get("key_header"):
                    errors.append(f"providers.{pname}: missing 'key_header'")
                chain = pdef.get("model_fallback", {}).get("chain")
                if chain is not None and not isinstance(chain, list):
                    errors.append(f"providers.{pname}: model_fallback.chain must be a list")

            # keys vs providers alignment
            unknown_providers = set(v_keys.keys()) - set(v_providers.keys())
            for up in unknown_providers:
                errors.append(f"keys.{up}: no matching provider defined under 'providers:'")

            if errors:
                for e in errors:
                    print(f"  [FAIL] {e}")
                sys.exit(1)

            print(f"  [OK]  Config file      : {CONFIG_FILE}")
            print(f"  [OK]  Server host:port : {host}:{port}")
            print(f"  [OK]  Log format       : {v_srv.get('log_format', 'text')}")
            print(f"  [OK]  State file       : {v_rl.get('state_file', 'rotakey_state.json')}")
            print(f"  [OK]  Log file         : {v_srv.get('log_file', 'rotakey.log')}")
            print(f"  [OK]  Providers        : {list(v_providers.keys())}")
            print()
            for pname, pdef in v_providers.items():
                pkeys  = v_keys.get(pname, []) or []
                chain  = pdef.get("model_fallback", {}).get("chain", [])
                status = "✓" if pkeys else "⚠ no keys"
                print(f"  [{status}] {pname:<12} {len(pkeys)} key(s)  {len(chain)} fallback model(s)")
                for m in chain[:3]:
                    print(f"          ├─ {m}")
                if len(chain) > 3:
                    print(f"          └─ … and {len(chain) - 3} more")

            total_k = sum(len(v or []) for v in v_keys.values())
            # also count env-injected keys
            env_k = sum(
                len([k for k in (v_keys.get(p) or []) if not any(
                    k in (yaml.safe_load(open(CONFIG_FILE).read()) or {}).get("keys", {}).get(p, [])
                    for _ in [None]
                )])
                for p in v_providers
            )
            print(f"\n  Total keys configured : {total_k}")
            print("  Config is VALID — no problems found.\n")
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"  [FAIL] {exc}\n")
            sys.exit(1)

    # ── --dry-run: validate + connectivity probe every provider's base_url ─────
    if "--dry-run" in sys.argv:
        import asyncio as _asyncio
        import httpx as _httpx

        print("\n  RotaKey dry-run\n  " + "─" * 40)
        try:
            dr_cfg       = _load_yaml_from_disk()
            dr_providers = dr_cfg.get("providers", {})
            dr_keys      = dr_cfg.get("keys", {})
        except Exception as exc:
            print(f"  [FAIL] Could not load config: {exc}\n")
            sys.exit(1)

        async def _probe():
            results = []
            async with _httpx.AsyncClient(timeout=10.0) as client:
                for pname, pdef in dr_providers.items():
                    url   = pdef.get("base_url", "")
                    pkeys = dr_keys.get(pname, []) or []
                    try:
                        r = await client.head(url, follow_redirects=True)
                        ok  = r.status_code < 500
                        msg = f"HTTP {r.status_code}"
                    except Exception as e:
                        ok  = False
                        msg = str(e)
                    sym    = "✓" if ok else "✗"
                    kcount = len(pkeys)
                    results.append((sym, pname, url, msg, kcount))
            return results

        rows = _asyncio.run(_probe())
        any_fail = False
        for sym, pname, url, msg, kcount in rows:
            print(f"  [{sym}] {pname:<14} {kcount} key(s)  {url}  → {msg}")
            if sym == "✗":
                any_fail = True

        print()
        if any_fail:
            print("  One or more providers unreachable — check network or base_url.\n")
            sys.exit(1)
        else:
            print("  All providers reachable. Ready to start.\n")
            sys.exit(0)

            sys.exit(0)

    cfg = _cfg_sync()

    srv          = cfg.get("server", {})
    default_port = int(srv.get("port", 8765))
    port_env     = int(os.environ.get("ROTAKEY_PORT", default_port))

    if not _is_port_free(port_env):
        if "ROTAKEY_PORT" in os.environ:
            log.error("=" * 58)
            log.error(f"  PORT {port_env} IS ALREADY IN USE")
            log.error("  Fix: set a different port before starting:")
            log.error("       Windows: set ROTAKEY_PORT=8766 && python proxy.py")
            log.error("       Linux:   ROTAKEY_PORT=8766 python3 proxy.py")
            log.error("=" * 58)
            sys.exit(1)
        else:
            log.warning(f"Port {port_env} in use. Auto-selecting...")
            port = _find_free_port(port_env + 1)
            log.warning(
                f"Auto-selected port {port}. "
                f"Update client: baseUrl=http://localhost:{port}/openrouter/v1"
            )
    else:
        port = port_env

    _check_config_permissions()

    rl        = cfg.get("rate_limit", {})
    providers = _providers(cfg)
    all_keys  = cfg.get("keys", {})
    # ── Banner — 4-color palette ──────────────────────────────────────────────
    # GR gray  : borders, bullets, structural chrome
    # YL yellow: section titles, numeric stats, version
    # RD red   : warnings (auth off, dead keys, circuit breaker threshold)
    # BL blue  : URLs, provider names, route paths, model chains

    W = 72   # total banner width including border chars

    def _c(code: str) -> str:
        return code if _USE_COLOR else ""

    GR = _c(_C.GREY)
    YL = _c(_C.YELLOW)
    RD = _c(_C.RED)
    BL = _c(_C.BLUE)
    BD = _c(_C.BOLD)
    DM = _c(_C.DIM)
    WH = _c(_C.WHITE)
    RS = _c(_C.RESET)

    def _brdr(s: str) -> str:
        return f"{GR}{BD}{s}{RS}"

    def _top() -> str:
        return _brdr("╔" + "═" * (W - 2) + "╗")

    def _hdr() -> str:
        return _brdr("╠" + "═" * (W - 2) + "╣")

    def _bot() -> str:
        return _brdr("╚" + "═" * (W - 2) + "╝")

    def _emp() -> str:
        return _brdr("║") + " " * (W - 2) + _brdr("║")

    def _sect(title: str) -> str:
        """  ╠─── TITLE ──────────────────────────────────╣  """
        dashes_total = W - 4 - len(title)
        ld, rd = 3, dashes_total - 3
        return (
            _brdr("╠" + "─" * ld)
            + f" {YL}{BD}{title}{RS} "
            + _brdr("─" * rd + "╣")
        )

    def _cline(text: str, color: str) -> str:
        """Centered line inside the box."""
        padded = text.center(W - 2)
        return _brdr("║") + f"{color}{BD}{padded}{RS}" + _brdr("║")

    def _kv(label: str, value: str, vc: str = "") -> str:
        """  ║  ▸ label               value           ║  """
        vc  = vc or WH
        lw  = 18
        row = f"  ▸ {label:<{lw}}{value}"
        pad = W - 2 - len(row)
        if pad < 0:
            value = value[:W - 2 - 4 - lw - 1] + "…"
            row   = f"  ▸ {label:<{lw}}{value}"
            pad   = max(0, W - 2 - len(row))
        return (
            _brdr("║")
            + f"{GR}  ▸ {RS}"
            + f"{DM}{label:<{lw}}{RS}"
            + f"{vc}{value}{RS}"
            + " " * pad
            + _brdr("║")
        )

    def _sub(text: str, color: str = "") -> str:
        """Indented model-chain sub-row."""
        c   = color or DM
        row = f"       {text}"
        pad = W - 2 - len(row)
        if pad < 0:
            row = row[:W - 3] + "…"
            pad = 0
        return _brdr("║") + f"{c}{row}{RS}" + " " * pad + _brdr("║")

    # ── Pre-compute totals ─────────────────────────────────────────────────────
    total_keys   = sum(len(v) for v in all_keys.values() if v)
    total_models = sum(
        len(pdef.get("model_fallback", {}).get("chain", []))
        for pdef in providers.values()
    )
    active_providers = len([p for p in all_keys if all_keys.get(p)])

    auth_val   = (f"ENABLED  ·  ROTAKEY_TOKEN is set" if _ROTAKEY_TOKEN
                  else "DISABLED  —  set ROTAKEY_TOKEN env var")
    auth_color = YL if _ROTAKEY_TOKEN else RD

    # ── ASCII art (6 lines, 59 chars each, centered in W-2=70) ────────────────
    _ART = [
        "██████╗  ██████╗ ████████╗ █████╗ ██╗  ██╗███████╗██╗   ██╗",
        "██╔══██╗██╔═══██╗╚══██╔══╝██╔══██╗██║ ██╔╝██╔════╝╚██╗ ██╔╝",
        "██████╔╝██║   ██║   ██║   ███████║█████╔╝ █████╗   ╚████╔╝ ",
        "██╔══██╗██║   ██║   ██║   ██╔══██║██╔═██╗ ██╔══╝    ╚██╔╝  ",
        "██║  ██║╚██████╔╝   ██║   ██║  ██║██║  ██╗███████╗   ██║   ",
        "╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝   ╚═╝   ",
    ]

    # ── Build and print banner line by line ───────────────────────────────────
    print(flush=True)
    print(_top(), flush=True)
    print(_emp(), flush=True)
    for art_line in _ART:
        print(_cline(art_line, BL), flush=True)
    print(_emp(), flush=True)
    print(_cline("Smart API Key Rotation & Model Fallback Proxy", GR + DM), flush=True)
    print(_cline(f"v6  ·  {total_keys} keys  ·  {active_providers} provider(s)  ·  {total_models} fallback models", YL), flush=True)
    print(_emp(), flush=True)
    print(_hdr(), flush=True)

    # ENDPOINTS
    print(_sect("ENDPOINTS"), flush=True)
    print(_kv("Listening",   f"http://localhost:{port}",          BL), flush=True)
    print(_kv("Health",      f"http://localhost:{port}/health",    BL), flush=True)
    print(_kv("Status",      f"http://localhost:{port}/status",    BL), flush=True)
    print(_kv("Metrics",     f"http://localhost:{port}/metrics",   BL), flush=True)
    print(_kv("Config",      str(CONFIG_FILE.name),                WH), flush=True)
    print(_kv("State file",  str(rl.get("state_file", "rotakey_state.json")), WH), flush=True)
    print(_kv("Log file",    str(srv.get("log_file",  "rotakey.log")),        WH), flush=True)
    log_fmt = srv.get("log_format", "text")
    print(_kv("Log format",  log_fmt, YL), flush=True)
    print(_kv("Inbound auth", auth_val, auth_color), flush=True)

    # CAPACITY
    print(_sect("CAPACITY"), flush=True)
    print(_kv("API keys",        f"{total_keys} across {active_providers} provider(s)",                   YL), flush=True)
    print(_kv("Fallback models", f"{total_models} total",                                                  YL), flush=True)
    print(_kv("Circuit breaker", f"trip after {_CB_THRESHOLD} failures  ·  reset after {_CB_TRIP_SECS}s", RD), flush=True)
    print(_kv("Recovery window", f"{rl.get('recovery_window', 300)}s",                                    YL), flush=True)
    print(_kv("Backoff",         f"{rl.get('backoff_schedule', [60, 120, 300, 600])}s",                   YL), flush=True)

    # ROUTING
    print(_sect("ROUTING"), flush=True)
    for rank, (pname, pdef) in enumerate(providers.items(), start=1):
        pfx   = pdef.get("prefix", f"/{pname}")
        pkeys = all_keys.get(pname, [])
        chain = pdef.get("model_fallback", {}).get("chain", [])
        key_color = YL if pkeys else RD
        val = (
            f"{key_color}{len(pkeys)} keys{RS}  "
            f"{YL}{len(chain)} models{RS}  "
            f"{GR}→{RS} {BL}{pfx}/...{RS}"
        )
        # Compute visible length of val for padding (strip ANSI)
        import re as _re
        vis_val = _re.sub(r"\x1b\[[0-9;]*m", "", val)
        label   = f"[{rank}] {pname}"
        lw = 18
        row_vis = f"  ▸ {label:<{lw}}{vis_val}"
        pad = max(0, W - 2 - len(row_vis))
        print(
            _brdr("║")
            + f"{GR}  ▸ {RS}"
            + f"{DM}{label:<{lw}}{RS}"
            + val
            + " " * pad
            + _brdr("║"),
            flush=True,
        )
        show_more = len(chain) > 4
        for i, m in enumerate(chain[:4]):
            is_last_visible = (i == min(3, len(chain) - 1)) and not show_more
            branch = "└" if is_last_visible else "├"
            print(_sub(f"{branch}─ {m}", DM), flush=True)
        if show_more:
            print(_sub(f"└─ … and {len(chain) - 4} more model(s)", DM), flush=True)

    # LEGEND
    print(_sect("LEGEND"), flush=True)
    print(_kv("Log tags", "→ REQ  ✓ OK  ⤵ FBACK  ⤳ SPILL  ✗ 429  ↻ ROTATE", GR), flush=True)
    print(_kv("",         "↷ SKIP  ✗ DEAD  ⚠ EXHAUST  ⚡ C.BREAKER  ❄ COOL", GR), flush=True)
    print(_bot(), flush=True)
    print(flush=True)

    # ── FIX: wrap uvicorn.run so fatal exceptions print a real traceback
    # instead of just setting errorlevel 1 and showing the generic
    # "Proxy failed to start" message from start.bat / start.sh.
    # The most common cause was an unhandled exception bubbling out of the
    # ASGI streaming generator (GeneratorExit from a client disconnect that
    # was not caught inside _stream's finally block — now fixed above).
    try:
        uvicorn.run(
            app,
            host=srv.get("host", "127.0.0.1"),
            port=port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        log.info("RotaKey proxy stopped by user (Ctrl+C).")
        sys.exit(0)
    except Exception as _fatal:
        import traceback as _tb
        log.critical(
            f"FATAL: uvicorn crashed — {type(_fatal).__name__}: {_fatal}\n"
            + _tb.format_exc()
        )
        sys.exit(1)
