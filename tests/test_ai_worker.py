"""The hosted assistant's Worker (server/ai_worker): its copy of the shared prompt is current, its settings are the safe ones, and
its own tests (JavaScript, `node --test`) pass when Node is installed. The logic itself is tested in server/ai_worker/test."""

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parent.parent / "server/ai_worker"


def _sync():
    spec = importlib.util.spec_from_file_location("sync_prompt", WORKER / "sync_prompt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_workers_copy_of_the_prompt_is_current():
    for name, text in _sync().render().items():
        assert (WORKER / name).read_text(encoding="utf-8") == text, f"{name} is stale: run python3 server/ai_worker/sync_prompt.py"


def test_the_workers_golden_requests_are_what_the_app_really_sends():
    from cygnus.core.ai import prompt

    for fixture in _sync().FIXTURES:
        sent = _sync().client_request(fixture)
        assert sent == {"v": prompt.PROMPT_VERSION, "application": fixture["application"], "documents": fixture["documents"]}


def test_the_worker_is_set_up_for_one_job_on_the_free_plan_and_holds_no_secret():
    config = tomllib.loads((WORKER / "wrangler.toml").read_text())
    [migration] = config["migrations"]
    assert migration["new_sqlite_classes"] == ["Limiter"] and "new_classes" not in migration  # the free plan only allows SQLite
    assert config["durable_objects"]["bindings"] == [{"name": "LIMITER", "class_name": "Limiter"}]
    assert config["observability"]["enabled"] is False  # requests are not logged
    assert config["main"] == "src/index.js" and config["compatibility_date"]
    assert "GEMINI_API_KEY" not in config["vars"] and config["vars"]["DISABLED"] == "0"
    for path in [p for p in WORKER.rglob("*") if p.is_file() and "node_modules" not in p.parts and ".wrangler" not in p.parts]:
        assert not re.search(r"AIza[0-9A-Za-z_-]{20,}|AQ\.[0-9A-Za-z_-]{30,}", path.read_text(errors="ignore")), f"a key is written in {path}"


def test_the_numbers_in_the_configuration_are_the_numbers_the_code_defaults_to():
    config = tomllib.loads((WORKER / "wrangler.toml").read_text())["vars"]
    code = (WORKER / "src/logic.js").read_text()
    defaults = {name: int(eval(compile(ast.parse(value.replace("_", ""), mode="eval"), "x", "eval"), {"__builtins__": {}}))
                for name, value in re.findall(r'number\(env, "(\w+)", ([\d _*]+)\)', code)}
    assert set(defaults) == {"PER_IP_MIN", "PER_IP_HOUR", "GLOBAL_PER_MIN", "DAILY_CAP", "CACHE_TTL_S"}
    assert {name: int(config[name]) for name in defaults} == defaults


def test_the_worker_only_ever_contacts_the_one_model_address():
    hosts = set()
    for name in ("logic.js", "index.js"):
        hosts |= set(re.findall(r"https?://([\w.-]+)", (WORKER / "src" / name).read_text()))
    assert hosts == {"generativelanguage.googleapis.com"}


@pytest.mark.needs_tool("node")
def test_the_workers_own_tests_pass():
    done = subprocess.run(["node", "--test", "test/"], cwd=WORKER, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]


# -- the app and the Worker have to agree (round 11: three ways a real request could have been turned away) ---------------------------
def _worker_number(name: str) -> int:
    match = re.search(rf"export const {name} = ([\d _*]+);", (WORKER / "src/logic.js").read_text())
    return int(eval(compile(ast.parse(match.group(1).replace("_", ""), mode="eval"), "x", "eval"), {"__builtins__": {}}))


def _worker_regex(name: str) -> re.Pattern:
    body = re.search(rf"const {name} = /(.*)/;", (WORKER / "src/logic.js").read_text()).group(1)
    return re.compile(body)


def test_the_biggest_request_the_app_can_send_is_within_the_workers_size_limit(monkeypatch):
    from cygnus.core.ai import needs, prompt, providers, sources
    from cygnus.core.util import http

    sent = {}

    class Reply:
        status_code = 200

        def iter_content(self, size):
            yield b'{"answer": {"components": []}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_post(url, **kw):
        sent.update(kw)
        return Reply()

    monkeypatch.setattr(http.requests, "post", fake_post)
    limit = _worker_number("MAX_BODY")
    for char in ("\U0001f680", "日", "é", "a", '"', "\\", "\n"):
        pages = [sources.Page(f"https://x.example/{i}", char * prompt.MAX_DOCUMENT_CHARS) for i in range(prompt.MAX_DOCUMENTS)]
        providers.Hosted("https://ai.example").ask_needs(needs.AppInfo("X", "x.y", "flatpak", []), pages)
        assert len(sent["data"]) <= limit, f"{char!r}: {len(sent['data'])} bytes"
    # and what the old way (escaping every non-ASCII character) would have made of Japanese pages: too big
    assert len(json.dumps({"t": "日" * 2 * prompt.MAX_DOCUMENT_CHARS}).encode()) > 200 * 1024


def test_a_page_the_app_sends_never_holds_a_character_the_worker_turns_away():
    from cygnus.core.ai import sources

    refused = _worker_regex("CONTROL")
    every_control = bytes(range(0, 32)) + b"\x7f"
    text = sources.to_text(b"Install the tool " + every_control + b" first.")
    assert text and not refused.search(text)
    assert not refused.search(sources.to_text(b"<p>a" + every_control + b"b</p>"))


def test_the_facts_the_app_sends_about_a_program_are_always_what_the_worker_accepts():
    from cygnus.core.ai import needs, providers

    fmt, ident, control = _worker_regex("FORMAT"), _worker_regex("ID"), _worker_regex("CONTROL")
    odd = ["", "x", "My\nApp\x1b[2J", "x" * 500, "🚀" * 150, "a‮b", "\x00\x01", "tab\there", "  spaced   out  "]
    for name in odd:
        for app_id in ("", "org.x.Y", "has space", "a/b:c@d+e", "x" * 300, "ünï", None, 5):
            for fmt_name in ("flatpak", "appimage", "pacman", "aur", "Flatpak", "", "x" * 30, None):
                facts = providers.facts(needs.AppInfo(name, app_id, fmt_name, []))
                assert 1 <= len(facts["name"]) <= 100 and "\n" not in facts["name"] and not control.search(facts["name"])
                assert ident.fullmatch(facts["id"]) and fmt.fullmatch(facts["format"]), facts


def test_an_address_the_app_keeps_is_one_the_worker_accepts():
    from cygnus.core.ai import sources

    for url in ("https://vendor.example/docs/linux?x=1&y=2#top", "https://vendor.example:443/a", "https://raw.githubusercontent.com/o/r/HEAD/README.md"):
        kept = sources.clean_url(url)
        assert kept == url and 12 <= len(kept) <= 300 and not re.search(r"[\s\x85<>\"]", kept) and kept.startswith("https://")
