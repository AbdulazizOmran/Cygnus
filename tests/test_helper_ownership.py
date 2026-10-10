"""The helper's ledger says which packages Cygnus installed (a later removal of any other asks first). Going back to an older version
of a package from the cache must not make the ledger claim a package that is part of the system."""

import pytest

from cygnus.core.backends import pacman as pm
from cygnus.helper import actions

CONFIG = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=("core",), servers={}, hold=())


class Ledger:
    def __init__(self, owned=()):
        self.owned = set(owned)

    def owns_package(self, name):
        return name in self.owned


def local(name="konsole", version="1.0-1"):
    return {"path": "/staged/x.pkg.tar", "name": name, "version": version, "sha256": "0" * 64, "signed": False, "has_install_script": False}


def plan(ledger, installed, **req):
    return actions.plan_packages(req, caller_uid=1000, caller_sender=":1.1", ledger=ledger, local=[local()], config=CONFIG,
                                 satisfy=lambda deps: {}, installed=lambda names: {n: installed.get(n) for n in names},
                                 analyse=lambda targets, **kw: pm.PacmanAnalysis(targets=targets))


def recorded(p):
    return [op[1]["name"] for op in p.ledger_ops if op[0] == "record_package"]


def test_a_file_is_claimed_as_installed_by_cygnus_by_default():
    assert recorded(plan(Ledger(), {})) == ["konsole"]
    assert recorded(plan(Ledger(), {"konsole": {"version": "2.0-1"}})) == ["konsole"]  # replacing something, nobody said not to claim it


def test_a_package_only_taken_back_to_an_older_version_is_not_claimed_when_the_request_says_so():
    p = plan(Ledger(), {"konsole": {"version": "2.0-1"}}, claim_ownership=False)
    assert recorded(p) == []
    assert p.summary["local"][0]["replaces_installed"] == "2.0-1"


def test_a_package_the_ledger_already_holds_is_still_recorded_so_its_version_follows():
    assert recorded(plan(Ledger({"konsole"}), {"konsole": {"version": "2.0-1"}}, claim_ownership=False)) == ["konsole"]


def test_a_package_that_is_not_installed_yet_is_recorded_whatever_the_flag_says():
    assert recorded(plan(Ledger(), {}, claim_ownership=False)) == ["konsole"]


@pytest.mark.parametrize("value", ["no", 0, None, "False", [False]])
def test_only_exactly_false_declines_to_claim(value):
    assert recorded(plan(Ledger(), {"konsole": {"version": "2.0-1"}}, claim_ownership=value)) == ["konsole"]


def test_removal_after_such_a_downgrade_still_asks_about_a_package_cygnus_did_not_install():
    ledger = Ledger()
    p = plan(ledger, {"konsole": {"version": "2.0-1"}}, claim_ownership=False)
    for op, args in p.ledger_ops:  # what committing would do to the ledger
        if op == "record_package":
            ledger.owned.add(args["name"])
    with pytest.raises(actions.PlanRefused, match="not installed by Cygnus"):
        actions.plan_packages({"remove": ["konsole"]}, caller_uid=1000, caller_sender=":1.1", ledger=ledger, config=CONFIG)


def test_the_client_sends_the_flag_in_the_request_and_defaults_to_claiming():
    from gi.repository import GLib

    from cygnus.core.privilege import HelperClient

    sent = []
    client = HelperClient.__new__(HelperClient)
    client.GLib = GLib
    client.Gio = __import__("gi.repository", fromlist=["Gio"]).Gio
    client._call = lambda method, params, reply, **kw: sent.append((method, params.unpack())) or ("p1", "{}", "msg")
    client.plan_packages(remove=["x"])
    client.plan_packages(remove=["x"], claim_ownership=False)
    assert [req["claim_ownership"] for _method, (req, _handles, _shas) in sent] == [True, False]
