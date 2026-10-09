"""The HTTPS client: no request is ever made to a plain-HTTP address, even one reached by a redirect."""

import hashlib

import pytest

from cygnus.core.util import http


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code, self.headers, self._body, self.closed = status, headers or {}, body, False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def server(monkeypatch):
    """A fake network: url -> response. Records every address that was requested."""
    pages, requested = {}, []

    def fake_get(url, headers=None, timeout=None, stream=None, allow_redirects=None):
        assert allow_redirects is False  # redirects are followed by Cygnus itself
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(http.requests, "get", fake_get)
    return pages, requested


def test_a_redirect_to_plain_http_is_refused_before_it_is_contacted(server):
    pages, requested = server
    pages["https://a.example/x"] = FakeResponse(302, {"Location": "http://evil.example/payload"})
    pages["http://evil.example/payload"] = FakeResponse(200, body=b"never read")
    with pytest.raises(http.HttpError, match="non-HTTPS"):
        http.get("https://a.example/x")
    assert requested == ["https://a.example/x"]  # the plain-HTTP address was never requested


def test_https_redirects_are_followed_including_relative_ones(server):
    pages, requested = server
    pages["https://a.example/dir/x"] = FakeResponse(301, {"Location": "/other/y"})
    pages["https://a.example/other/y"] = FakeResponse(302, {"Location": "https://b.example/z"})
    pages["https://b.example/z"] = FakeResponse(200, body=b"hello")
    assert http.get("https://a.example/dir/x") == b"hello"
    assert requested == ["https://a.example/dir/x", "https://a.example/other/y", "https://b.example/z"]


def test_a_redirect_loop_ends(server):
    pages, requested = server
    pages["https://a.example/x"] = FakeResponse(302, {"Location": "https://a.example/x"})
    with pytest.raises(http.HttpError, match="too many redirects"):
        http.get("https://a.example/x")
    assert len(requested) == http.MAX_REDIRECTS + 1


def test_the_size_limit_still_applies_after_a_redirect(server):
    pages, _ = server
    pages["https://a.example/x"] = FakeResponse(302, {"Location": "https://b.example/big"})
    pages["https://b.example/big"] = FakeResponse(200, body=b"x" * 5000)
    with pytest.raises(http.HttpError, match="exceeds"):
        http.get("https://a.example/x", limit=1000)


def test_a_download_follows_https_redirects_and_verifies_the_digest(server, tmp_path):
    pages, requested = server
    body = b"package bytes" * 100
    pages["https://a.example/pkg.tar"] = FakeResponse(302, {"Location": "https://cdn.example/pkg.tar"})
    pages["https://cdn.example/pkg.tar"] = FakeResponse(200, {"Content-Length": str(len(body))}, body)
    path = http.download("https://a.example/pkg.tar", tmp_path, expected_sha256=hashlib.sha256(body).hexdigest())
    assert path.read_bytes() == body and requested[-1] == "https://cdn.example/pkg.tar"
    with pytest.raises(http.HttpError, match="checksum mismatch"):
        http.download("https://a.example/pkg.tar", tmp_path, expected_sha256="0" * 64)
    assert not list(tmp_path.glob(".*.part"))


def test_a_download_redirected_to_plain_http_never_contacts_it(server, tmp_path):
    pages, requested = server
    pages["https://a.example/pkg.tar"] = FakeResponse(307, {"Location": "http://mirror.example/pkg.tar"})
    dest = tmp_path / "downloads"
    with pytest.raises(http.HttpError, match="non-HTTPS"):
        http.download("https://a.example/pkg.tar", dest)
    assert requested == ["https://a.example/pkg.tar"] and not list(dest.iterdir())


def test_the_first_url_must_be_https_too():
    with pytest.raises(http.HttpError, match="non-HTTPS"):
        http.get("http://a.example/x")


def test_a_download_never_writes_through_a_link_left_where_its_partial_file_goes(server, tmp_path):
    pages, _ = server
    body = b"payload"
    pages["https://h.example/a.deb"] = FakeResponse(200, {"Content-Length": str(len(body))}, body)
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (tmp_path / ".a.deb.part").symlink_to(victim)  # planted where the partial file will be created
    path = http.download("https://h.example/a.deb", tmp_path)
    assert path.read_bytes() == body and victim.read_text() == "precious"


def test_a_folder_that_cannot_be_written_is_a_plain_error(server, tmp_path):
    pages, _ = server
    pages["https://h.example/a.deb"] = FakeResponse(200, {}, b"x")
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        import os

        if os.geteuid() == 0:
            return
        with pytest.raises(http.HttpError, match="could not save the download"):
            http.download("https://h.example/a.deb", folder)
    finally:
        folder.chmod(0o700)
