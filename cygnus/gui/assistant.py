"""The optional AI assistant as the windows and the terminal use it: its settings and key, and "what else does this need?"."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from cygnus.core.ai import config, keystore, needs, providers, sources
from cygnus.core.ai.text import plain
from cygnus.core.errors import CygnusError
from cygnus.core.registry import db as regdb
from cygnus.core.util import http, proc

_FLATHUB_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")
_PKG = re.compile(r"[a-z0-9@_+][a-z0-9@._+-]{0,99}")
_REPO_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def _scope(settings: dict[str, Any]) -> str:
    """A key for a server of the person's own is kept under that server's address (see keystore._purpose)."""
    return settings["base_url"] if settings["provider"] == "compatible" else ""


def status() -> dict[str, Any]:
    settings = config.load()
    ok, why = keystore.available()
    return {**settings, "providers": config.PROVIDERS, "has_key": keystore.has_key(settings["provider"], _scope(settings)) if ok else False,
            "wallet_ok": ok, "wallet_why": why, "default_models": config.DEFAULT_MODELS, "hosted_available": bool(config.hosted_url())}


def configure(enabled: bool, provider: str, model: str, base_url: str = "") -> dict[str, Any]:
    before = config.load()
    config.save(enabled=bool(enabled), provider=provider, model=model, base_url=base_url)
    after = config.load()
    if before["provider"] == "compatible" and before["base_url"] and before["base_url"] != after["base_url"] and provider == "compatible":
        try:  # the key was for the old address: it is not carried over to the new one
            keystore.clear_key("compatible", before["base_url"])
        except keystore.KeystoreError:
            pass
    return status()


def _key_scope(provider: str) -> str:
    if provider not in ("gemini", "compatible"):
        raise CygnusError("only Gemini and your own server use a key")
    settings = config.load()
    if provider == "compatible":
        if settings["provider"] != "compatible" or not settings["base_url"]:
            raise CygnusError("set the server's address first (a key is kept for one server only)")
        return settings["base_url"]
    return ""


def set_key(provider: str, key: str) -> dict[str, Any]:
    keystore.set_key(provider, key, _key_scope(provider))
    return status()


def clear_key(provider: str) -> dict[str, Any]:
    keystore.clear_key(provider, _key_scope(provider))
    return status()


def test(progress: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Do the settings work? The shared assistant is only asked whether it is ready; the others get one harmless question.
    Nothing about the person is sent either way."""
    provider = providers.from_settings()
    if isinstance(provider, providers.Hosted):
        progress(f"Asking {provider.name} whether it is ready…")
        return {"ok": provider.check(), "provider": provider.name}
    progress(f"Asking {provider.name} a harmless test question…")
    answer = provider.generate_json("Answer with JSON only.", 'Return exactly {"ok": true}.')
    return {"ok": isinstance(answer, dict) and answer.get("ok") is True, "provider": provider.name}


def _flathub_only(host: str) -> bool:
    return host in ("flathub.org", "www.flathub.org")


def _flathub_urls(app_id: str) -> list[str]:
    if not _FLATHUB_ID.fullmatch(app_id):
        return []
    try:  # its redirects too are only followed to flathub.org itself
        data = http.get_json(f"https://flathub.org/api/v2/appstream/{app_id}", limit=2 * 1024 * 1024, timeout=20.0, host_ok=_flathub_only)
    except CygnusError:
        return []
    urls = data.get("urls") if isinstance(data, dict) else None
    urls = urls if isinstance(urls, dict) else {}
    return [u for u in (sources.clean_url(urls.get(k)) for k in ("homepage", "help", "vcs_browser")) if u]


def _installation(installation_id: str, registry: Any = None) -> dict[str, Any]:
    from cygnus.core.registry import open_registry

    row = next((r for r in regdb.list_installations(registry or open_registry()) if r["id"] == installation_id), None)
    if row is None:
        raise CygnusError("unknown installation")
    return row


def homepages_for(row: dict[str, Any]) -> list[str]:
    """Where to read about an installed application: from its own metadata, never guessed."""
    from cygnus.core.manifest import catalog

    urls: list[str] = []
    source = row["source"]
    manifest = catalog.bundled_manifests().get(row["app_id"])
    if manifest is not None and manifest.manifest.application.homepage:
        urls.append(manifest.manifest.application.homepage)
    if row["format"] == "flatpak":
        parts = (source.get("ref") or "").split("/")
        if len(parts) > 1:
            urls += _flathub_urls(parts[1])
    elif row["format"] == "appimage":
        info = source.get("update_info") or {}
        owner, repo = info.get("owner"), info.get("repo")
        if str(info.get("type", "")).startswith("gh-releases") and isinstance(owner, str) and isinstance(repo, str) \
                and _REPO_PART.fullmatch(owner) and _REPO_PART.fullmatch(repo) and ".." not in owner + repo:
            urls.append(f"https://github.com/{owner}/{repo}")
    elif row["format"] in ("pacman", "aur"):
        name = source.get("package") or ""
        if _PKG.fullmatch(name):
            res = proc.run(["pacman", "-Qi", "--", name], timeout=30)
            found = re.search(r"^URL\s*:\s*(\S+)", res.stdout, re.M)
            if found:
                urls.append(found.group(1))
    return [u for u in dict.fromkeys(sources.clean_url(u) for u in urls) if u]


def discover(installation_id: str, extra_url: str, progress: Callable[[str], None], registry: Any = None) -> dict[str, Any]:
    """What else does this installed application need? (The documents are read from its own website; the person may add one.)"""
    row = _installation(installation_id, registry)
    provider = providers.from_settings()
    homepages = homepages_for(row)
    if extra_url.strip():
        extra = sources.clean_url(extra_url)
        if extra is None:
            raise CygnusError("that is not an https address")
        homepages.insert(0, extra)
    app = needs.AppInfo(row["name"], row["app_id"], row["format"], homepages)
    result = needs.discover(app, provider, progress=progress)
    return {"suggestions": [s.as_dict() for s in result.suggestions], "left_out": result.left_out, "pages": result.pages,
            "provider": result.provider, "app": plain(row["name"], 100) or "this application", "needs_address": not homepages}
