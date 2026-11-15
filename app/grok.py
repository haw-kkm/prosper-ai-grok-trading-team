"""Optional Grok (xAI) connection. RESEARCH ONLY.

Grok never places, sizes or approves trades and never changes risk limits. It only
returns research that the agents validate before showing it. If XAI_API_KEY is
missing or a call fails, the team keeps running on its built-in rules.

Calls happen only when you ask (dashboard buttons / POST /api/grok/...), or on a
timer if you opt in with GROK_AUTO_RESEARCH_SECONDS. One request per call, no
automatic retries. API reference: https://docs.x.ai (Chat Completions, Models).
"""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("grok")

TIMEOUT_SECONDS = 45            # reasoning models can take a while; never wait forever
MAX_COMPLETION_TOKENS = 1200    # visible answer only (docs: reasoning tokens are not counted here)

_lock = threading.Lock()


def _new_status():
    return {"last_attempt": None, "last_ok": None, "last_success": None, "last_error": None, "requests": 0, "failures": 0,
            "last_response_model": None, "last_usage": None}


_status = _new_status()


def redact(text):
    """Strip the API key (and anything shaped like an xAI key) and xAI team ids (billing errors quote them) from text
    we log or show."""
    text = str(text)
    if config.XAI_API_KEY:
        text = text.replace(config.XAI_API_KEY, "[redacted]")
    text = re.sub(r"(?i)(team\W{0,20})[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r"\1[redacted]", text)
    return re.sub(r"xai-[A-Za-z0-9_\-]{8,}", "[redacted]", text)


def status():
    """Connection state for the dashboard. Never contains the API key."""
    with _lock:
        s = dict(_status)
    if not config.grok_enabled():
        state = "OFFLINE DEMO"
    elif s["last_attempt"] is None:
        state = "NOT VERIFIED"
    else:
        state = "CONNECTED" if s["last_ok"] else "ERROR"
    return {"state": state, "model": config.XAI_MODEL if config.grok_enabled() else None, **s}


def _record(ok, kind=None, message="", **extra):
    now = time.time()
    with _lock:
        _status["last_attempt"], _status["last_ok"] = now, ok
        if ok:  # the model id xAI says answered, and token usage when the response includes it
            _status["last_success"] = now
            _status["last_response_model"] = extra.get("response_model")
            _status["last_usage"] = extra.get("usage") if isinstance(extra.get("usage"), dict) else None
        else:
            _status["failures"] += 1
            _status["last_error"] = {"time": now, "kind": kind, "message": redact(message)[:300], **extra}
    if not ok:
        log.warning("Grok %s: %s", kind, redact(message)[:300])


def _error_detail(err):
    """Short, redacted error text from an xAI error body (format isn't documented, so be defensive)."""
    try:
        body = json.loads(err.read(2000).decode("utf-8", "replace"))
        detail = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code") or ""
        return redact(detail)[:200]
    except Exception:
        return ""


def _request(method, path, body=None):
    """Send ONE HTTPS request to xAI. Returns (json, None) or (None, (kind, message, extra))."""
    if not config.XAI_BASE_URL.startswith("https://"):
        return None, ("config", "XAI_BASE_URL must start with https:// so the key is never sent unencrypted.", {})
    req = urllib.request.Request(
        config.XAI_BASE_URL + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.XAI_API_KEY}", "Content-Type": "application/json"},
        method=method,
    )
    with _lock:
        _status["requests"] += 1
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as err:  # before URLError: HTTPError is a subclass
        kind = {401: "auth", 403: "auth", 404: "not_found", 429: "rate_limit"}.get(
            err.code, "server" if err.code >= 500 else "bad_request")
        extra = {"http_status": err.code}
        if err.code == 429 and err.headers and err.headers.get("Retry-After"):
            extra["retry_after"] = str(err.headers.get("Retry-After"))[:20]
        detail = _error_detail(err)
        return None, (kind, f"HTTP {err.code}" + (f": {detail}" if detail else ""), extra)
    except TimeoutError:
        return None, ("timeout", f"No answer within {TIMEOUT_SECONDS}s.", {})
    except urllib.error.URLError as err:
        if isinstance(err.reason, TimeoutError):
            return None, ("timeout", f"No answer within {TIMEOUT_SECONDS}s.", {})
        return None, ("network", f"Network error ({type(err.reason).__name__}).", {})
    except ValueError:  # body was not JSON / not UTF-8
        return None, ("bad_response", "xAI returned a body that is not JSON.", {})
    except Exception as err:
        return None, ("network", f"Unexpected error ({type(err).__name__}).", {})


def ask_grok_json(system_prompt, payload, schema_name, schema, validate):
    """Ask Grok for structured research about `payload`.

    `validate(data)` must return the cleaned result or raise ValueError. Returns the
    validated result, or None if Grok is off or anything failed (recorded as ERROR,
    never as success).
    """
    if not config.grok_enabled():
        return None
    data, err = _request("POST", "/chat/completions", {
        "model": config.XAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload)},
        ],
        "temperature": 0.2,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
    })
    if err:
        kind, message, extra = err
        _record(False, kind, message, **extra)
        return None
    try:
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("answer was cut off at the token limit")
        if choice["message"].get("refusal"):
            raise ValueError("Grok refused the request")
        result = validate(json.loads(choice["message"]["content"]))
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
        _record(False, "bad_response", f"Unusable answer: {e}")
        return None
    _record(True, response_model=data.get("model"), usage=data.get("usage"))
    return result


def test_connection():
    """ONE request that checks the key and model without generating text: GET /v1/models/{model}."""
    if not config.grok_enabled():
        return status()
    data, err = _request("GET", "/models/" + urllib.parse.quote(config.XAI_MODEL, safe=""))
    if err:
        kind, message, extra = err
        _record(False, kind, message, **extra)
    elif not isinstance(data, dict) or not data.get("id"):
        _record(False, "bad_response", "Model lookup returned no model id.")
    else:
        _record(True, response_model=data["id"])
    return status()
