"""Asking a model for JSON. Two kinds of server are understood: Google Gemini (with the person's own key) and anything that
speaks the common OpenAI-style chat protocol (a local Ollama, for example). The key travels in a header, never in the
address, is never logged, and an answer is only ever data: it is parsed as JSON and handed to code that checks it."""

from __future__ import annotations

import json
import re
from typing import Any

from urllib.parse import quote

from cygnus.core.ai import config, keystore, prompt
from cygnus.core.ai.text import plain
from cygnus.core.errors import CygnusError
from cygnus.core.util import http

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_ANSWER = 512 * 1024
MAX_OUTPUT_TOKENS = 4096


class AiError(CygnusError):
    """Something went wrong asking the model, in words a person can act on."""


def _explain(exc: http.HttpError, service: str) -> AiError:
    status = exc.status
    detail = ""
    try:  # a service's own explanation is shown only in a short, plain form
        detail = plain(json.loads(exc.body).get("error", {}).get("message", ""), 200)  # a server chose these words: no escape sequences
    except Exception:  # noqa: BLE001
        detail = ""
    if status in (400, 401, 403):
        base = f"{service} did not accept the key or the request"
    elif status == 404:
        base = f"{service} does not know that model name (change it in Settings)"
    elif status == 429:
        base = f"{service} says the usage limit has been reached; try again later"
    elif status is not None and status >= 500:
        base = f"{service} is having trouble right now; try again later"
    else:
        base = f"could not reach {service}"
    return AiError(base + (f": {detail}" if detail else "") + ("" if status else f" ({plain(str(exc), 200)})"))


def _json_from_text(text: Any) -> Any:
    if not isinstance(text, str):
        raise AiError("the answer was not in the form that was asked for")
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AiError("the answer was not in the form that was asked for") from exc


def _docs(pages) -> list[dict[str, str]]:
    return [{"url": p.url, "text": p.text} for p in pages]


_ID = re.compile(r"[A-Za-z0-9._:/@+-]{0,255}")
_FORMAT = re.compile(r"[a-z][a-z0-9-]{0,19}")


def facts(app) -> dict[str, str]:
    """What is said about the application: its name, id and format, in the plain form the hosted service accepts (an odd name or id
    is shortened or left out rather than turning the whole request away)."""
    ident = app.app_id if isinstance(app.app_id, str) and _ID.fullmatch(app.app_id) else ""
    fmt = app.fmt if isinstance(app.fmt, str) and _FORMAT.fullmatch(app.fmt) else "unknown"
    return {"name": plain(app.name, 100) or "this application", "id": ident, "format": fmt}


class Hosted:
    """Cygnus's own service: it holds the key, limits use, remembers answers, and builds the message itself from the structured
    facts below, so that it can only be used for this one job. What is sent: the application's name, id and format, and the
    public documentation text Cygnus fetched; nothing else about the person or the computer."""

    name = "the Cygnus assistant"

    def __init__(self, base_url: str):
        self.base = base_url

    def check(self) -> bool:
        """Is the service reachable and ready? (Nothing is asked of the model and nothing about the person is sent.)"""
        try:
            data = http.get_json(f"{self.base}/healthz", limit=4096, timeout=20.0)
        except http.HttpError as exc:
            raise AiError(f"Could not reach the shared assistant ({exc})") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise AiError("the shared assistant did not answer as expected")
        if data.get("ready") is not True:
            raise AiError("The shared assistant is switched off or not set up right now; try again later, or use your own key (Settings)")
        return True

    def ask_needs(self, app, pages) -> Any:
        body = {"v": prompt.PROMPT_VERSION, "application": facts(app), "documents": _docs(pages)}
        try:
            data = http.post_json(f"{self.base}/v1/needs", body, limit=MAX_ANSWER, timeout=120.0)
        except http.HttpError as exc:
            raise _hosted_error(exc) from None
        answer = data.get("answer") if isinstance(data, dict) else None
        if not isinstance(answer, dict):
            raise AiError("the Cygnus assistant's answer was not in the expected form")
        return answer


def _hosted_error(exc: http.HttpError) -> AiError:
    try:
        said = plain(json.loads(exc.body).get("error", ""), 200)
    except Exception:  # noqa: BLE001
        said = ""
    status = exc.status
    if status == 429:
        base = "The shared assistant has reached its limit for now; try again later, or use your own key (Settings)"
    elif status == 426:
        base = "This version of Cygnus is too old for the assistant: update Cygnus"
    elif status in (503, 502):
        base = "The shared assistant is unavailable right now; try again later, or use your own key (Settings)"
    elif status == 400:
        base = "The assistant could not use that request"
    else:
        base = "Could not reach the shared assistant"
    return AiError(base + (f" ({said})" if said and status in (400, 426) else "") + ("" if status else f" ({plain(str(exc), 200)})"))


class Gemini:
    name = "Google Gemini"

    def ask_needs(self, app, pages) -> Any:
        return self.generate_json(prompt.SYSTEM, prompt.build_user(facts(app), _docs(pages)))

    def __init__(self, key: str, model: str):
        self.key, self.model = key, model

    def generate_json(self, system: str, user: str, schema: dict | None = None) -> Any:
        generation: dict[str, Any] = {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": MAX_OUTPUT_TOKENS}
        if schema is not None:
            generation["responseSchema"] = schema
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": generation}
        try:
            data = http.post_json(GEMINI_URL.format(model=quote(self.model, safe="")), body, headers={"x-goog-api-key": self.key},
                                  limit=MAX_ANSWER, timeout=90.0)
        except http.HttpError as exc:
            raise _explain(exc, self.name) from None  # the key is in a header, but nothing from the request is carried along
        try:
            if (data.get("promptFeedback") or {}).get("blockReason"):
                raise AiError("Gemini declined to answer this request")
            candidate = data["candidates"][0]
            text = "".join(part.get("text", "") for part in candidate["content"]["parts"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise AiError("Gemini's answer was empty or not in the expected form") from exc
        return _json_from_text(text)


class Compatible:
    name = "the AI server"

    def ask_needs(self, app, pages) -> Any:
        return self.generate_json(prompt.SYSTEM, prompt.build_user(facts(app), _docs(pages)))

    def __init__(self, base_url: str, key: str | None, model: str):
        self.base, self.key, self.model = base_url, key, model

    def generate_json(self, system: str, user: str, schema: dict | None = None) -> Any:
        body = {"model": self.model, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            data = http.post_json(f"{self.base}/chat/completions", body, headers=headers, limit=MAX_ANSWER, timeout=180.0,
                                  allow_loopback_http=True)
        except http.HttpError as exc:
            raise _explain(exc, self.name) from None
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AiError("the AI server's answer was empty or not in the expected form") from exc
        return _json_from_text(text)


def from_settings(settings: dict[str, Any] | None = None) -> Hosted | Gemini | Compatible:
    """The provider the person set up. Raises AiError when it is not usable yet."""
    settings = settings or config.load()
    if not settings["enabled"]:
        raise AiError("the AI assistant is switched off (Settings, AI assistant)")
    if settings["provider"] == "hosted":
        if not config.hosted_url():
            raise AiError("the shared assistant is not available yet: choose your own key (Settings, AI assistant)")
        return Hosted(config.hosted_url())
    if settings["provider"] == "gemini":
        try:
            key = keystore.get_key("gemini")
        except keystore.KeystoreError as exc:
            raise AiError(str(exc)) from exc
        if not key:
            raise AiError("no Gemini key is saved yet (Settings, AI assistant)")
        return Gemini(key, settings["model"])
    if not settings["base_url"]:
        raise AiError("no server address is set (Settings, AI assistant)")
    try:  # a local server often needs no key: a wallet that does not answer is then no reason to stop
        key = keystore.get_key("compatible", settings["base_url"])
    except keystore.KeystoreError:
        key = None
    return Compatible(settings["base_url"], key, settings["model"])
