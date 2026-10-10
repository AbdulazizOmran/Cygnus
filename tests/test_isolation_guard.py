"""Tests must never undo conftest's isolation (XDG, runtime and Flatpak dirs pointing at tmp)."""

import re
from pathlib import Path


def test_no_test_undoes_the_isolation():
    offenders = []
    for f in Path(__file__).parent.glob("test_*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bmonkeypatch\.undo\(\)", code):
                offenders.append(f"{f.name}:{n}")
    assert offenders == [], "use pytest.MonkeyPatch.context() instead: " + ", ".join(offenders)


def test_every_xdg_directory_points_into_the_test_folder(tmp_path):
    import os

    from cygnus.core import paths

    for d in (paths.data_dir(), paths.config_dir(), paths.cache_dir(), paths.state_dir(), paths.runtime_dir()):
        assert str(d).startswith(str(tmp_path)), d
    assert os.environ["FLATPAK_USER_DIR"].startswith(str(tmp_path))


def test_a_test_that_tries_to_reach_the_internet_fails_at_once_and_this_computer_still_works():
    import socket
    import threading

    import pytest

    with pytest.raises(OSError, match="do not use the internet"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(OSError, match="do not use the internet"):
        socket.create_connection(("2606:2800:220:1:248:1893:25c8:1946", 80), timeout=1)
    with pytest.raises(socket.gaierror, match="do not use the internet"):
        socket.getaddrinfo("example.org", 443)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    with socket.create_connection(server.getsockname(), timeout=2):  # a server a test starts for itself is fine
        pass
    server.close()
    assert socket.getaddrinfo("localhost", 80)


def test_datagrams_to_the_internet_are_refused_too_and_look_alike_names_are_not_taken_for_numbers():
    import socket

    import pytest

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError, match="do not use the internet"):
            udp.sendto(b"x", ("93.184.216.34", 53))
        with pytest.raises(OSError, match="do not use the internet"):
            udp.sendto(b"x", 0, ("93.184.216.34", 53))
        with pytest.raises(OSError, match="do not use the internet"):
            udp.sendmsg([b"x"], [], 0, ("93.184.216.34", 53))
        with pytest.raises(OSError, match="do not use the internet"):
            udp.connect(("93.184.216.34", 53))
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        udp.sendto(b"hello", receiver.getsockname())  # to this computer: fine
        assert receiver.recv(10) == b"hello"
        receiver.close()
    finally:
        udp.close()
    for host in ("١.٢.٣.٤", "example١.org", "1.2.3.4.example.org"):  # digits of other scripts are not an address
        with pytest.raises(socket.gaierror, match="do not use the internet"):
            socket.getaddrinfo(host, 443)
