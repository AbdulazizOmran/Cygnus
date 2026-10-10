import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_tool(name): skip unless the tool is installed")


def pytest_runtest_setup(item):
    for mark in item.iter_markers(name="needs_tool"):
        for tool in mark.args:
            if shutil.which(tool) is None:
                pytest.skip(f"requires '{tool}'")


@pytest.fixture(scope="session")
def _flatpak_system(tmp_path_factory):
    # libflatpak reads these once per process, so they must stay the same for the whole session.
    root = tmp_path_factory.mktemp("flatpak-system")
    (root / "config").mkdir()
    return root


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch):
    """No test reaches the internet: a connection to anything but this computer fails at once, with a message that says so, and a name
    is not looked up unless the test fakes the lookup itself. (A test that needs a server starts its own on 127.0.0.1.)"""
    import socket

    import re

    local = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
    real_connect, real_connect_ex, real_lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    real_sendto, real_sendmsg = socket.socket.sendto, socket.socket.sendmsg

    def refuse(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not (isinstance(address, tuple) and str(address[0]) in local):
            raise OSError("the tests do not use the internet: fake the network call (tests/conftest.py)")

    def connect(self, address):
        refuse(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        refuse(self, address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        refuse(self, args[-1])  # sendto(data, address) and sendto(data, flags, address)
        return real_sendto(self, data, *args)

    def sendmsg(self, buffers, *args):
        if len(args) >= 3 and args[2]:
            refuse(self, args[2])
        return real_sendmsg(self, buffers, *args)

    def lookup(host, *args, **kwargs):
        if isinstance(host, str) and host not in local and not re.fullmatch(r"[0-9.]+", host) and ":" not in host:
            raise socket.gaierror("the tests do not use the internet: fake the network call (tests/conftest.py)")
        return real_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch, _flatpak_system):
    """Never let a test write to (or read) the real user's XDG directories or Flatpak installations."""
    for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_STATE_HOME", "state")):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / sub))
    runtime = tmp_path / "xdg" / "run"
    runtime.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("FLATPAK_USER_DIR", str(tmp_path / "xdg" / "flatpak-user"))
    monkeypatch.setenv("FLATPAK_SYSTEM_DIR", str(_flatpak_system / "system"))
    monkeypatch.setenv("FLATPAK_CONFIG_DIR", str(_flatpak_system / "config"))
