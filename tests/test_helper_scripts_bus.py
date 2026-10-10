"""The administrator-script action end to end on a private D-Bus with a fake polkit (no root; the command is faked, so nothing runs)."""

import os

import pytest

from cygnus.core.privilege import HelperError
from cygnus.core.util import scriptrun
from tests.test_helper_bus import Bus

pytestmark = pytest.mark.needs_tool("dbus-daemon")
BODY = b"#!/bin/sh\necho hello\n"
SHA = scriptrun.sha256(BODY)


@pytest.fixture
def bus(tmp_path):
    folder = tmp_path / "prog"
    folder.mkdir()
    b = Bus(tmp_path)
    b.folder = str(folder)
    yield b
    b.close()


def test_a_script_plan_goes_through_authorisation_and_the_one_command_is_what_was_planned(bus):
    plan = bus.client.plan_script("postinst.sh", BODY, SHA, bus.folder)
    assert plan.summary["sha256"] == SHA and plan.summary["script"] == "postinst.sh" and "as administrator" in plan.message
    lines: list[str] = []
    ok, detail = bus.client.commit(plan, on_progress=lines.append)
    assert ok, detail
    ran = [line for line in lines if line.startswith("[fake] systemd-run")]
    assert len(ran) == 1 and "echo hello" in ran[0] and f"--working-directory={bus.folder}" in ran[0] and "--unit=cygnus-script-" in ran[0]
    [call] = bus.polkit_calls()
    assert call["action"] == "io.github.omranabdulaziz.Cygnus.scripts.run" and call["message"] == plan.message and call["interactive"]
    [op] = bus.client.ledger()
    assert op["kind"] == "script" and op["state"] == "succeeded" and SHA in op["summary"] and "echo hello" not in op["summary"]


def test_every_run_asks_again_and_a_plan_is_used_once_and_only_by_who_made_it(bus):
    from cygnus.core.privilege import HelperClient

    for _ in range(2):
        ok, _ = bus.client.commit(bus.client.plan_script("postinst.sh", BODY, SHA, bus.folder))
        assert ok
    assert len(bus.polkit_calls()) == 2  # nothing is remembered from the first
    plan = bus.client.plan_script("postinst.sh", BODY, SHA, bus.folder)
    with pytest.raises(HelperError, match="another caller"):
        HelperClient(bus.address).commit(plan)
    assert bus.client.commit(plan)[0]
    with pytest.raises(HelperError, match="unknown or expired"):
        bus.client.commit(plan)


@pytest.mark.parametrize("args, message", [
    (("postinst.sh", BODY, "0" * 64, None), "not the script that was shown"),
    (("postinst.sh", b"#!/usr/bin/ruby\n", scriptrun.sha256(b"#!/usr/bin/ruby\n"), None), "run it yourself"),
    (("postinst.sh", b"", scriptrun.sha256(b""), None), "empty"),
    (("bad\nname", BODY, SHA, None), "not a script name"),
    (("postinst.sh", BODY, SHA, "/no/such/folder"), "not there"),
    (("postinst.sh", BODY, SHA, "relative"), "absolute"),
])
def test_a_request_that_is_not_exactly_the_shown_script_is_refused_over_the_wire(bus, args, message):
    name, data, sha, folder = args
    with pytest.raises(HelperError, match=message):
        bus.client.plan_script(name, data, sha, folder or bus.folder)
    assert bus.polkit_calls() == [] and bus.client.ledger() == []


def test_a_denied_authorisation_runs_nothing(tmp_path):
    (tmp_path / "prog").mkdir()
    b = Bus(tmp_path, allow=False)
    try:
        ok, detail = b.client.commit(b.client.plan_script("postinst.sh", BODY, SHA, str(tmp_path / "prog")))
        assert not ok and "denied" in detail and b.client.ledger() == []
    finally:
        b.close()
