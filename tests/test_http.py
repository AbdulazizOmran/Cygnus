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

    def fake_get(url, headers=None, timeout=None, stream=None, allow_redirects=None, auth=None):
        assert allow_redirects is False  # redirects are followed by Cygnus itself
        assert auth is not None  # an explicit (empty) authentication: ~/.netrc is never read for a page request
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


# -- what the host check is made on, and how long a request may take ---------------------------------------------------------------
@pytest.mark.parametrize("location", ["https://127.0.0.1\\@vendor.example/payload", "https://evil.example\\@vendor.example/x",
                                      "https://user:pw@vendor.example/x", "https://vendor.example/a b", "https://vendor.example/\x7f",
                                      "https://[::1/x", "https://vendor.example/\u0085"])
def test_an_address_that_two_parsers_read_differently_is_refused_before_it_is_requested_even_when_its_host_would_pass(server, location):
    pages, requested = server
    pages["https://vendor.example/start"] = FakeResponse(302, {"Location": location})
    with pytest.raises(http.HttpError):
        http.get("https://vendor.example/start", host_ok=lambda host: host == "vendor.example")
    assert requested == ["https://vendor.example/start"]  # nothing was contacted at the odd address


def test_the_host_check_is_made_on_every_hop_by_the_real_redirect_code(server):
    pages, requested = server
    pages["https://vendor.example/a"] = FakeResponse(302, {"Location": "https://cdn.vendor.example/b"})
    pages["https://cdn.vendor.example/b"] = FakeResponse(302, {"Location": "https://elsewhere.example/c"})
    pages["https://elsewhere.example/c"] = FakeResponse(200, body=b"never read")
    seen = []

    def ok(host):
        seen.append(host)
        return host.endswith("vendor.example")

    with pytest.raises(http.HttpError, match="not allowed"):
        http.get("https://vendor.example/a", host_ok=ok)
    assert seen == ["vendor.example", "cdn.vendor.example", "elsewhere.example"]
    assert requested == ["https://vendor.example/a", "https://cdn.vendor.example/b"]  # the third was never requested


def test_the_host_that_is_checked_is_the_host_that_is_connected_to():
    from urllib3.util import parse_url

    for url in ("https://vendor.example/ok", "https://Vendor.Example:8443/ok", "https://[2001:db8::1]/ok"):
        connects = parse_url(http.requests.Request("GET", url).prepare().url).host
        assert http._checked_host(url).strip("[]") == connects.strip("[]").lower()


def test_a_request_that_is_answered_too_slowly_is_given_up_whatever_the_socket_timeout(monkeypatch):
    import threading

    stuck = threading.Event()
    with pytest.raises(http.HttpError, match="too slowly"):
        http._within(0.05, lambda: stuck.wait(30), "https://slow.example/")
    stuck.set()
    assert http._within(5, lambda: 42, "x") == 42
    with pytest.raises(ZeroDivisionError):  # an error of the work itself comes through unchanged
        http._within(5, lambda: 1 / 0, "x")


# -- post_json ---------------------------------------------------------------------------------------------------------------------
class FakePost:
    """requests.post stand-in: records what was sent and answers with `reply`."""

    def __init__(self, status=200, body=b"{}", said=b""):
        self.status_code, self._body, self.said, self.sent = status, body, said, {}

        class Raw:
            def read(inner, size, decode_content=False):
                return self.said

        self.raw = Raw()

    def __call__(self, url, **kw):
        self.sent = {"url": url, **kw}
        return self

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_post_sends_the_text_as_it_is_in_utf8_and_never_follows_a_redirect_or_reads_netrc(monkeypatch):
    fake = FakePost(body=b'{"ok": true}')
    monkeypatch.setattr(http.requests, "post", fake)
    assert http.post_json("https://api.example/v1", {"text": "日本語 \U0001F680 é"}, headers={"x-key": "k"}) == {"ok": True}
    assert fake.sent["data"] == '{"text": "日本語 \U0001F680 é"}'.encode()  # not \\uXXXX escapes: they make Japanese text six times bigger
    assert fake.sent["allow_redirects"] is False and fake.sent["auth"] is not None and fake.sent["proxies"] is None
    assert fake.sent["headers"]["x-key"] == "k" and fake.sent["headers"]["Content-Type"] == "application/json; charset=utf-8"


def test_a_key_sent_to_a_local_server_is_never_handed_to_a_proxy(monkeypatch):
    fake = FakePost()
    monkeypatch.setattr(http.requests, "post", fake)
    http.post_json("http://localhost:11434/v1", {}, headers={"Authorization": "Bearer k"}, allow_loopback_http=True)
    assert fake.sent["proxies"] == {"http": None, "https": None}  # whatever HTTP_PROXY says


def test_an_error_status_carries_what_the_server_said_and_a_broken_answer_is_a_plain_error(monkeypatch):
    fake = FakePost(status=429, said=b'{"error": "slow down"}')
    monkeypatch.setattr(http.requests, "post", fake)
    with pytest.raises(http.HttpError) as caught:
        http.post_json("https://api.example/v1", {})
    assert caught.value.status == 429 and "slow down" in caught.value.body
    for body in (b"not json", b"[" * 200_000, b""):  # the deep one overflows the JSON reader's stack
        monkeypatch.setattr(http.requests, "post", FakePost(body=body))
        with pytest.raises(http.HttpError, match="invalid JSON"):
            http.post_json("https://api.example/v1", {})


def test_a_post_to_an_address_with_a_user_name_or_odd_characters_is_refused(monkeypatch):
    monkeypatch.setattr(http.requests, "post", lambda *a, **k: pytest.fail("must not be sent"))
    for url in ("https://user@api.example/v1", "https://api.example\\@evil.example/v1", "https://api.example/a b"):
        with pytest.raises(http.HttpError):
            http.post_json(url, {})
