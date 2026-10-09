"""Acting on an AI suggestion: only what Cygnus itself checked and remembered, only through the helper's usual plans."""

import pytest

from cygnus.core.ai import needs
from cygnus.core.errors import CygnusError
from cygnus.core.privilege import Plan
from cygnus.gui import assistant, fixes


def _remember(kind, target, *, satisfied="", name="Thing"):
    s = needs.Suggestion(token=f"tok-{kind}-{target}", name=name, relation="required", why="because", kind=kind, target=target,
                         source="extra", citation_url="https://vendor.example/doc", quote="the quoted sentence", satisfied=satisfied)
    needs.remember([s])
    return s.token


class FakeClient:
    calls: list = []

    def plan_packages(self, **kw):
        FakeClient.calls.append(("packages", kw))
        return Plan("p1", {}, "Install " + ", ".join(kw["install_repo"]))

    def plan_group(self, group, op):
        FakeClient.calls.append(("group", group, op))
        return Plan("p2", {}, f"Add you to {group}")

    def plan_unit(self, unit, action):
        FakeClient.calls.append(("unit", unit, action))
        return Plan("p3", {}, f"Start {unit}")


@pytest.fixture(autouse=True)
def _helper(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(fixes, "HelperClient", FakeClient)
    yield
    for token in list(fixes._PENDING):
        fixes._PENDING.pop(token, None)


def test_a_package_suggestion_becomes_an_ordinary_confirmed_install():
    out = fixes.plan_suggestion(_remember("package", "libfoo"))
    assert FakeClient.calls == [("packages", {"install_repo": ["libfoo"]})]  # explicit, not "as a dependency"
    assert out["kind"] == "helper" and out["token"] in fixes._PENDING and "AI" in out["security_note"] and out["relogin"] is False


def test_a_group_suggestion_asks_the_helper_to_add_the_user_and_says_to_log_in_again():
    out = fixes.plan_suggestion(_remember("group", "input"))
    assert FakeClient.calls == [("group", "input", "add")] and out["relogin"] is True


def test_a_service_suggestion_asks_the_helper_to_enable_and_start_it():
    out = fixes.plan_suggestion(_remember("service", "foo.service"))
    assert FakeClient.calls == [("unit", "foo.service", "enable_now")] and out["kind"] == "helper"


def test_an_extension_suggestion_only_opens_a_store_page_and_asks_nothing_of_the_helper():
    out = fixes.plan_suggestion(_remember("extension", "https://addons.mozilla.org/firefox/addon/foo/"))
    assert out["kind"] == "browser" and out["urls"] == ["https://addons.mozilla.org/firefox/addon/foo/"] and FakeClient.calls == []


@pytest.mark.parametrize("target", ["https://evil.example/foo", "https://addons.mozilla.org.evil.example/x", "http://addons.mozilla.org/x"])
def test_an_extension_address_that_is_not_a_store_page_is_refused(target):
    with pytest.raises(CygnusError):
        fixes.plan_suggestion(_remember("extension", target))
    assert FakeClient.calls == []


@pytest.mark.parametrize("kind, target", [("group", "wheel"), ("group", "root"), ("package", "--overwrite=*"), ("package", "Foo Bar"),
                                          ("service", "../x.service"), ("service", "foo"), ("whatever", "x")])
def test_what_does_not_fit_its_shape_is_refused_before_the_helper_is_asked(kind, target):
    with pytest.raises(CygnusError):
        fixes.plan_suggestion(_remember(kind, target))
    assert FakeClient.calls == []


@pytest.mark.parametrize("kind", ["info", "aur"])
def test_a_note_and_a_community_package_are_not_acted_on_here(kind):
    with pytest.raises(CygnusError):
        fixes.plan_suggestion(_remember(kind, "x" if kind == "aur" else ""))
    assert FakeClient.calls == []


def test_something_already_in_place_is_not_planned_again():
    with pytest.raises(CygnusError, match="nothing to do"):
        fixes.plan_suggestion(_remember("package", "libfoo", satisfied="it is already installed"))
    assert FakeClient.calls == []


@pytest.mark.parametrize("token", ["", "nope", None, 5, "tok-package-libfoo-not-remembered"])
def test_only_a_token_cygnus_handed_out_can_be_acted_on(token):
    with pytest.raises(CygnusError):
        fixes.plan_suggestion(token)
    assert FakeClient.calls == []


def test_the_settings_say_whether_the_hosted_assistant_has_an_address(monkeypatch):
    monkeypatch.setattr("cygnus.core.ai.config.HOSTED_URL", "")
    monkeypatch.delenv("CYGNUS_AI_URL", raising=False)
    assert assistant.status()["hosted_available"] is False
    monkeypatch.setenv("CYGNUS_AI_URL", "https://ai.example.test")
    assert assistant.status()["hosted_available"] is True


# -- the terminal side --------------------------------------------------------------------------------------------------------

import argparse  # noqa: E402
import io  # noqa: E402
import sys  # noqa: E402

from cygnus.cli import main as cli  # noqa: E402
from cygnus.core.ai import config, keystore  # noqa: E402
from tests.test_ai_core import FakeWallet  # noqa: E402


@pytest.fixture
def wallet(monkeypatch):
    fake = FakeWallet()
    monkeypatch.setattr(keystore, "_wallet", fake)
    return fake


def test_the_terminal_turns_it_on_picks_an_assistant_and_keeps_the_key_in_the_wallet_only(wallet, monkeypatch, capsys):
    assert cli.cmd_ai(argparse.Namespace(sub="status")) == 0
    assert "assistant: off" in capsys.readouterr().out
    assert cli.cmd_ai(argparse.Namespace(sub="use", provider="gemini", model=None, base_url=None)) == 0
    assert config.load()["provider"] == "gemini" and config.load()["model"] == config.DEFAULT_MODELS["gemini"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("AIzaSyExampleKey123\n"))
    assert cli.cmd_ai(argparse.Namespace(sub="key", action="set", provider="gemini")) == 0
    assert wallet.items and "AIzaSyExampleKey123" in wallet.items.values()
    assert cli.cmd_ai(argparse.Namespace(sub="on")) == 0 and config.load()["enabled"] is True
    capsys.readouterr()
    assert cli.cmd_ai(argparse.Namespace(sub="status")) == 0
    shown = capsys.readouterr().out
    assert "assistant: on" in shown and "key saved: yes" in shown and "AIzaSy" not in shown  # the key is never printed
    assert cli.cmd_ai(argparse.Namespace(sub="key", action="clear", provider="gemini")) == 0 and not wallet.items
    assert cli.cmd_ai(argparse.Namespace(sub="off")) == 0 and config.load()["enabled"] is False


def test_switching_to_the_hosted_assistant_drops_the_model_and_keeps_the_on_off_choice(wallet):
    config.save(enabled=True, provider="gemini", model="gemini-flash-lite-latest")
    cli.cmd_ai(argparse.Namespace(sub="use", provider="hosted", model=None, base_url=None))
    assert config.load() == {"enabled": True, "provider": "hosted", "model": "", "base_url": ""}


def test_asking_what_a_program_needs_is_refused_while_the_assistant_is_off_and_for_unknown_programs(monkeypatch, capsys):
    monkeypatch.setattr(cli, "open_registry", lambda path=None: object())
    from cygnus.core.registry import db as regdb

    monkeypatch.setattr(regdb, "list_installations", lambda reg: [{"id": "i1", "name": "Hello", "app_id": "org.hello"}])
    assert cli.cmd_needs(argparse.Namespace(app="Nothing", url=None, json=False, registry=None)) == 1
    assert "not installed through Cygnus" in capsys.readouterr().err
    assert cli.cmd_needs(argparse.Namespace(app="hello", url=None, json=False, registry=None)) == 1
    assert "assistant is off" in capsys.readouterr().err


def test_the_terminal_lists_checked_suggestions_with_their_quotes_and_installs_nothing(monkeypatch, capsys):
    config.save(enabled=True, provider="hosted", model="")
    monkeypatch.setattr(cli, "open_registry", lambda path=None: object())
    from cygnus.core.registry import db as regdb

    monkeypatch.setattr(regdb, "list_installations", lambda reg: [{"id": "i1", "name": "Hello", "app_id": "org.hello"}])
    seen = {}

    def fake(installation_id, extra_url, progress, registry=None):
        seen.update(id=installation_id, url=extra_url)
        return {"app": "Hello", "provider": "hosted", "needs_address": False, "pages": ["https://hello.example/"], "left_out": ["one was dropped"],
                "suggestions": [{"name": "libfoo", "relation": "required", "kind": "package", "why": "it needs foo", "satisfied": "",
                                 "quote": "needs libfoo", "citation_url": "https://hello.example/", "notes": ["from extra"]}]}

    monkeypatch.setattr(assistant, "discover", fake)
    monkeypatch.setattr(fixes, "HelperClient", lambda: pytest.fail("the terminal listing must not ask the helper"))
    assert cli.cmd_needs(argparse.Namespace(app="hello", url="https://hello.example/docs", json=False, registry=None)) == 0
    out = capsys.readouterr().out
    assert seen == {"id": "i1", "url": "https://hello.example/docs"}
    assert "libfoo (required, package)" in out and "needs libfoo" in out and "left out: one was dropped" in out and "confirmed there" in out


# -- "Test the connection" ----------------------------------------------------------------------------------------------------
from cygnus.core.ai import providers  # noqa: E402
from cygnus.core.util import http  # noqa: E402


def test_the_shared_assistant_is_tested_by_asking_whether_it_is_ready_and_no_model_call_is_made(monkeypatch):
    config.save(enabled=True, provider="hosted", model="")
    monkeypatch.setenv("CYGNUS_AI_URL", "https://ai.example.test")
    seen = []
    monkeypatch.setattr(http, "get_json", lambda url, **kw: seen.append(url) or {"ok": True, "ready": True})
    monkeypatch.setattr(http, "post_json", lambda *a, **k: pytest.fail("a test of the connection must not ask the model"))
    out = assistant.test()
    assert out == {"ok": True, "provider": "the Cygnus assistant"} and seen == ["https://ai.example.test/healthz"]


@pytest.mark.parametrize("reply, message", [({"ok": True, "ready": False}, "switched off or not set up"),
                                            ({"ok": True}, "switched off or not set up"),
                                            ({"ok": False, "ready": True}, "did not answer as expected"),
                                            ("nonsense", "did not answer as expected"),
                                            ([], "did not answer as expected")])
def test_a_shared_assistant_that_is_off_or_odd_is_said_so_in_words(monkeypatch, reply, message):
    config.save(enabled=True, provider="hosted", model="")
    monkeypatch.setenv("CYGNUS_AI_URL", "https://ai.example.test")
    monkeypatch.setattr(http, "get_json", lambda url, **kw: reply)
    with pytest.raises(providers.AiError, match=message):
        assistant.test()


def test_a_shared_assistant_that_cannot_be_reached_is_said_so(monkeypatch):
    config.save(enabled=True, provider="hosted", model="")
    monkeypatch.setenv("CYGNUS_AI_URL", "https://ai.example.test")

    def down(url, **kw):
        raise http.HttpError("HTTP 530 for " + url, 530)

    monkeypatch.setattr(http, "get_json", down)
    with pytest.raises(providers.AiError, match="Could not reach the shared assistant"):
        assistant.test()


def test_the_other_providers_are_tested_with_one_harmless_question_that_holds_nothing_about_the_person(wallet, monkeypatch):
    config.save(enabled=True, provider="gemini", model="gemini-flash-lite-latest")
    keystore.set_key("gemini", "AIzaFake0123456789")
    asked = []

    def fake(url, payload, **kw):
        asked.append(payload)
        return {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}]}

    monkeypatch.setattr(http, "post_json", fake)
    assert assistant.test() == {"ok": True, "provider": "Google Gemini"}
    [body] = asked
    assert body["contents"][0]["parts"][0]["text"] == 'Return exactly {"ok": true}.'


def test_nothing_is_tested_while_the_assistant_is_off():
    with pytest.raises(providers.AiError, match="switched off"):
        assistant.test()
