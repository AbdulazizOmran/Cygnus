"""What the independent review of the AI assistant (round 11) found, one test each: keys tied to their server, wallets that do not
answer, text that a server or a page chose, the address rules, and the request the hosted service has to accept."""

import json

import pytest

from cygnus.core.ai import config, keystore, needs, providers, sources
from cygnus.core.errors import CygnusError
from cygnus.core.util import http
from cygnus.gui import assistant
from tests.test_ai_core import Capture, FakeWallet


@pytest.fixture
def wallet(monkeypatch):
    fake = FakeWallet()
    monkeypatch.setattr(keystore, "_wallet", fake)
    return fake


def _compatible(base):
    return assistant.configure(True, "compatible", "llama3.1", base)


def test_a_key_is_kept_for_the_server_it_was_given_for_and_never_sent_to_another_address(wallet):
    _compatible("https://server-a.example/v1")
    assistant.set_key("compatible", "sk-SECRET-A")
    assert providers.from_settings().key == "sk-SECRET-A" and assistant.status()["has_key"] is True
    _compatible("https://attacker.example/v1")  # changed in Settings, in the terminal, or by a hand-edited file: the same result
    assert providers.from_settings().key is None and assistant.status()["has_key"] is False
    assert not wallet.items  # and the key of the old address was removed
    _compatible("https://server-a.example/v1")
    assert providers.from_settings().key is None  # it is not coming back by itself: the person gives it again


def test_a_hand_edited_address_does_not_inherit_the_key(wallet):
    from cygnus.core import preferences

    _compatible("https://server-a.example/v1")
    assistant.set_key("compatible", "sk-SECRET-A")
    prefs = preferences.load()
    prefs["ai"]["base_url"] = "https://attacker.example/v1"  # not through configure(): nothing could clear the key
    preferences.save(prefs)
    assert providers.from_settings().key is None


def test_a_key_for_a_server_needs_the_server_to_be_set_first(wallet):
    config.save(enabled=True, provider="gemini", model="")
    with pytest.raises(CygnusError, match="address first"):
        assistant.set_key("compatible", "sk-x")
    with pytest.raises(CygnusError, match="only Gemini"):
        assistant.set_key("hosted", "sk-x")
    assistant.set_key("gemini", "AIzaFake0123456789")  # the Gemini address is fixed: no scope needed
    assert keystore.get_key("gemini") == "AIzaFake0123456789"


class BrokenWallet(FakeWallet):
    def lookup(self, purpose):
        raise RuntimeError("the wallet does not answer")


def test_a_wallet_that_does_not_answer_stops_gemini_but_not_a_local_server_that_needs_no_key(monkeypatch):
    monkeypatch.setattr(keystore, "_wallet", BrokenWallet())
    config.save(enabled=True, provider="gemini", model="")
    with pytest.raises(providers.AiError, match="wallet could not be read"):
        providers.from_settings()
    _compatible("http://localhost:11434/v1")
    assert providers.from_settings().key is None  # Ollama needs none


def test_a_missing_wallet_library_is_said_so_and_not_reported_as_no_key(monkeypatch):
    def broken():
        raise keystore.KeystoreError("the wallet library (libsecret) is not installed, so a key cannot be kept safely")

    monkeypatch.setattr(keystore, "_backend", broken)
    config.save(enabled=True, provider="gemini", model="")
    with pytest.raises(providers.AiError, match="libsecret"):
        providers.from_settings()
    assert keystore.has_key("gemini") is False  # the status page just says there is no key


@pytest.mark.parametrize("model", ["gemini-x/../../../v1beta/files", "a/b", "../x", "x:y", "x@y", "x..y", "-x", "", "x" * 101])
def test_a_gemini_model_name_cannot_change_the_path_of_the_address(model):
    if model == "":
        assert config.save(enabled=True, provider="gemini", model=model)["model"] == config.DEFAULT_MODELS["gemini"]
        return
    with pytest.raises(config.AiConfigError):
        config.save(enabled=True, provider="gemini", model=model)


def test_a_local_server_may_name_a_model_with_a_folder_and_a_tag_and_a_bad_gemini_name_in_the_file_is_ignored():
    from cygnus.core import preferences

    assert config.save(enabled=True, provider="compatible", model="library/llama3:8b", base_url="http://localhost:11434")["model"] == "library/llama3:8b"
    prefs = preferences.load()
    prefs["ai"] = {"enabled": True, "provider": "gemini", "model": "gemini-x/../../v1beta/files"}
    preferences.save(prefs)
    assert config.load()["model"] == config.DEFAULT_MODELS["gemini"]


def test_the_model_is_quoted_into_the_gemini_address(monkeypatch):
    capture = Capture(answer={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})
    monkeypatch.setattr(http, "post_json", capture)
    providers.Gemini("AIzaKey", "weird/../name").generate_json("s", "u")  # even a name that got past the settings
    assert "/weird%2F..%2Fname:generateContent" in capture.calls[0][0]


@pytest.mark.parametrize("base", ["http://localhost.evil.example/v1", "http://127.0.0.1.evil.com/v1", "http://localhost@evil.com/v1", "http://evil.com\\@localhost/v1",
                                  "http://0.0.0.0:11434/v1", "http://127.1/v1", "ftp://localhost/v1", "http://example.org/v1", "https://u:p@example.org/v1",
                                  "https://example.org/v1?key=1", "https://example.org/v1#x"])
def test_only_https_or_this_computer_may_be_the_server(base):
    with pytest.raises(config.AiConfigError):
        config.save(enabled=True, provider="compatible", model="m", base_url=base)


@pytest.mark.parametrize("base", ["http://localhost:11434/v1", "http://127.0.0.1:11434", "http://[::1]:11434/v1", "https://example.org/v1", "http://LOCALHOST/v1"])
def test_this_computer_and_https_servers_are_accepted(base):
    assert config.save(enabled=True, provider="compatible", model="m", base_url=base)["base_url"] == base.rstrip("/")


def test_an_answer_whose_content_is_not_text_is_a_plain_error_not_a_crash(monkeypatch):
    for content in ({"ok": True}, ["a"], 5, None):
        monkeypatch.setattr(http, "post_json", Capture(answer={"choices": [{"message": {"content": content}}]}))
        with pytest.raises(providers.AiError):
            providers.Compatible("https://example.org/v1", None, "m").generate_json("s", "u")


def test_what_a_server_says_when_it_refuses_never_carries_a_terminal_escape(monkeypatch):
    body = json.dumps({"error": {"message": "bad \x1b[2J\x1b]0;pwned\x07 text ‮"}})
    err = providers._explain(http.HttpError("HTTP 400", 400, body), "the server")
    assert "\x1b" not in str(err) and "\x07" not in str(err) and "‮" not in str(err) and "bad" in str(err)
    hosted = providers._hosted_error(http.HttpError("HTTP 400", 400, json.dumps({"error": "oops \x1b[31m red"})))
    assert "\x1b" not in str(hosted)
    unreachable = providers._explain(http.HttpError("network error \x1b[2J for x"), "the server")
    assert "\x1b" not in str(unreachable)


def test_the_facts_sent_about_an_application_are_always_in_the_form_the_hosted_service_accepts():
    app = needs.AppInfo("My\nApp\x1b[2J" + "x" * 200, "has space and \x00 bad", "Flatpak!", [])
    f = providers.facts(app)
    assert "\n" not in f["name"] and "\x1b" not in f["name"] and len(f["name"]) <= 100 and f["name"].startswith("My App")
    assert f["id"] == "" and f["format"] == "unknown"
    ok = providers.facts(needs.AppInfo("WhatPulse", "org.whatpulse.WhatPulse", "flatpak", []))
    assert ok == {"name": "WhatPulse", "id": "org.whatpulse.WhatPulse", "format": "flatpak"}
    assert providers.facts(needs.AppInfo("", "", "appimage", []))["name"] == "this application"


# -- addresses and pages -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://vendor.example:444/a", "https://vendor.example./a", 'https://vendor.example/a"b', "https://vendor.example/a<b",
                                 "https://x.example/a b", "https://x.example/a﻿b", "https://[::1", "https://a]b/", "https://u@vendor.example/",
                                 "https://vendor.example\\@evil.example/", "http://vendor.example/", "https://", "", None, 5])
def test_an_address_with_anything_odd_in_it_is_not_one_to_read(url):
    assert sources.clean_url(url) is None


def test_an_ordinary_address_and_its_usual_port_are_fine():
    assert sources.clean_url("  https://vendor.example/docs/linux?x=1  ") == "https://vendor.example/docs/linux?x=1"
    assert sources.clean_url("https://vendor.example:443/") == "https://vendor.example:443/"


@pytest.mark.parametrize("address", ["::127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "2002:0a00:1::1", "127.0.0.1", "10.0.0.5",
                                     "169.254.169.254", "192.168.1.1", "fe80::1", "fc00::1", "[::1]", "0.0.0.0", "::"])
def test_an_address_that_is_not_on_the_public_internet_is_never_read_in_any_spelling(address):
    assert sources._public(address) is False


def test_public_addresses_are_read():
    assert sources._public("8.8.8.8") is True and sources._public("2606:4700:4700::1111") is True and sources._public("::ffff:8.8.8.8") is True


def test_the_check_of_a_host_refuses_a_private_address_even_when_the_name_is_allowed(monkeypatch):
    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    assert sources.host_check({"vendor.example"})("vendor.example") is False
    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    assert sources.host_check({"vendor.example"})("vendor.example") is False  # one private answer among public ones is enough to refuse
    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert sources.host_check({"vendor.example"})("vendor.example") is True


@pytest.mark.parametrize("host", ["evil.sourceforge.net", "other.github.io", "x.workers.dev", "victim.herokuapp.com", "evil.github.com", "evilvendor.example"])
def test_a_page_on_a_shared_hosting_site_does_not_open_the_whole_site(host):
    for homepage in ("https://sourceforge.net/projects/x", "https://github.io/x", "https://workers.dev/", "https://herokuapp.com/", "https://github.com/o/r", "https://vendor.example/"):
        allowed = sources.vendor_hosts([homepage])
        assert not sources._allowed(host, allowed), (homepage, host)
    assert sources._allowed("cdn.vendor.example", sources.vendor_hosts(["https://vendor.example/"]))  # an ordinary vendor keeps its subdomains
    assert sources._allowed("sourceforge.net", sources.vendor_hosts(["https://sourceforge.net/projects/x"]))  # the exact host is still read


def test_page_text_has_no_control_characters_and_the_hosted_service_accepts_what_is_sent():
    text = sources.to_text(b"Install the tool\x01 first\x7f now.\x0b Then\x00 go.")
    assert text == "Install the tool first now. Then go." and not sources._NOT_TEXT.search(text)
    assert sources.to_text(b"\x00" * 20 + b"binary") == ""


def test_a_hostile_servers_words_in_a_problem_are_cleaned_before_they_are_shown(monkeypatch):
    def get(url, **kw):
        raise http.HttpError("redirected to \x1b[2J‮ evil \x07")

    monkeypatch.setattr(sources, "host_check", lambda allowed: (lambda host: True))
    with pytest.raises(sources.SourceError) as caught:
        sources.fetch_pages(["https://vendor.example/"], get=get)
    assert "\x1b" not in str(caught.value) and "‮" not in str(caught.value) and "\x07" not in str(caught.value)


def test_an_appimage_whose_update_information_names_a_odd_owner_or_repo_gives_no_address(monkeypatch):
    monkeypatch.setattr("cygnus.core.manifest.catalog.bundled_manifests", lambda: {})
    base = {"app_id": "x.y", "format": "appimage", "name": "X"}
    good = {**base, "source": {"update_info": {"type": "gh-releases-zsync", "owner": "some-owner", "repo": "some.repo"}}}
    assert assistant.homepages_for(good) == ["https://github.com/some-owner/some.repo"]
    for owner, repo in (("a/b", "r"), ("o", "../x"), ("o", "r r"), ("..", "r"), ("o", ""), (5, "r"), ("o", None), ("o\n", "r")):
        bad = {**base, "source": {"update_info": {"type": "gh-releases-zsync", "owner": owner, "repo": repo}}}
        assert assistant.homepages_for(bad) == [], (owner, repo)


def test_the_flathub_description_is_only_followed_to_flathub_itself():
    assert assistant._flathub_only("flathub.org") and assistant._flathub_only("www.flathub.org")
    assert not assistant._flathub_only("evil.example") and not assistant._flathub_only("flathub.org.evil.example") and not assistant._flathub_only("dl.flathub.org")


def test_the_address_shown_for_a_suggestion_is_only_ever_a_page_that_was_fetched():
    page = sources.Page("https://whatpulse.org/help", "WhatPulse needs the input group to see your keyboard.")
    comp = {"name": "Input", "relation": "required", "why": "keys", "action": {"kind": "group", "name": "input"},
            "citation": {"url": "https://whatpulse.org/help", "quote": "needs the input group to see your keyboard"}}
    check = dict(packages=lambda names: {}, aur=lambda names: set(), in_group=lambda g: False)
    [s], _ = needs.validate({"components": [comp]}, [page], **check)
    assert s.citation_url == page.url and s.citation_url.startswith("https://")
    for url in ("https://whatpulse.org/help#x", "https://whatpulse.org/help/", "HTTPS://WHATPULSE.ORG/help", "file:///etc/passwd", "javascript:alert(1)", "https://evil.example/help"):
        kept, left = needs.validate({"components": [{**comp, "citation": {**comp["citation"], "url": url}}]}, [page], **check)
        assert kept == [] and left, url
