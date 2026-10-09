"""The AI settings (in the preferences: nothing secret here; the key is in the wallet, see keystore.py)."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from cygnus.core import preferences
from cygnus.core.errors import CygnusError

# Where the hosted assistant runs (server/ai_worker, a Cloudflare Worker). CYGNUS_AI_URL overrides it, for testing.
HOSTED_URL = "https://cygnus-ai.cygnus-ai.workers.dev"

PROVIDERS = {
    "hosted": "Cygnus's hosted assistant (no key needed)",
    "gemini": "Google Gemini (your own key)",
    "compatible": "A local or other OpenAI-compatible server (for example Ollama)",
}
DEFAULT_MODELS = {"hosted": "", "gemini": "gemini-flash-lite-latest", "compatible": "llama3.1"}
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}")
_GEMINI_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # it becomes a part of the address: no "/", no "..", no ":" or "@"


def model_ok(provider: str, model: str) -> bool:
    """A local server may name a model "library/llama3:8b"; Gemini's name is a path segment of an address and must stay one."""
    if provider == "gemini":
        return bool(_GEMINI_MODEL.fullmatch(model)) and ".." not in model
    return bool(_MODEL.fullmatch(model))


class AiConfigError(CygnusError):
    pass


def _normal_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlparse(url)
    loopback = parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
    if not (parts.scheme == "https" or loopback) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise AiConfigError("the server address must be https, or http to this computer itself (localhost), with no password in it")
    return url


def hosted_url() -> str:
    """The address of the hosted assistant (https only), or "" when there is none."""
    url = (os.environ.get("CYGNUS_AI_URL") or HOSTED_URL).strip().rstrip("/")
    return url if url.startswith("https://") and " " not in url else ""


def load() -> dict[str, Any]:
    raw = preferences.load().get("ai")
    raw = raw if isinstance(raw, dict) else {}
    provider = raw.get("provider") if raw.get("provider") in PROVIDERS else "hosted"
    model = raw.get("model") if isinstance(raw.get("model"), str) and model_ok(provider, raw["model"]) else DEFAULT_MODELS[provider]
    if provider == "hosted":
        model = ""  # the hosted assistant chooses its own model
    base = ""
    if provider == "compatible" and isinstance(raw.get("base_url"), str):
        try:
            base = _normal_base(raw["base_url"])
        except AiConfigError:
            base = ""
    return {"enabled": raw.get("enabled") is True, "provider": provider, "model": model, "base_url": base}


def save(*, enabled: bool, provider: str, model: str, base_url: str = "") -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise AiConfigError("unknown provider")
    model = (model or "").strip() or DEFAULT_MODELS[provider]
    if model and not model_ok(provider, model):
        raise AiConfigError("the model name has characters it should not have (letters, digits and . _ - for Gemini; also : / @ for a local server)")
    base = ""
    if provider == "compatible":
        base = _normal_base(base_url)
    prefs = preferences.load()
    prefs["ai"] = {"enabled": bool(enabled), "provider": provider, "model": model, **({"base_url": base} if base else {})}
    preferences.save(prefs)
    return load()
