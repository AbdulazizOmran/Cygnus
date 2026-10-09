"""Arguments from a browser or the file manager are hostile: they only ever open the Install page with something to analyse."""

import os

import pytest

from cygnus.gui import launch, service

FLATHUB_REF = """[Flatpak Ref]
Name=org.mozilla.firefox
Branch=stable
Title=org.mozilla.firefox from flathub
IsRuntime=false
Url=https://dl.flathub.org/repo/
SuggestRemoteName=flathub
GPGKey=mQINBFlD2sABEADsiUZUOYBg1UdDaWkEdJYkTSZD68214m8Q1fbrP5AptaUfCl8KYKFMNoAJRBXn9FbE6q6VBzghHXj
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
"""


def _ref(tmp_path, text, name="app.flatpakref"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize("arg", ["appstream:org.mozilla.firefox", "appstream://org.mozilla.firefox", "APPSTREAM:org.mozilla.firefox",
                                 "appstream://org.mozilla.firefox/"])
def test_a_software_link_opens_that_app_from_flathub(arg):
    assert launch.resolve(arg, "/") == launch.Launch({"appSpec": "flathub:org.mozilla.firefox"})


@pytest.mark.parametrize("arg", ["appstream:", "appstream:../../etc/passwd", "appstream:org.x.App;rm -rf", "appstream:org", "appstream:-h",
                                 "appstream://org.x.App//stable/../x", "appstream:org.x.App\nExec=evil", "appstream:" + "a." * 400 + "b"])
def test_a_software_link_that_is_not_an_app_id_opens_nothing(arg):
    result = launch.resolve(arg, "/")
    assert result.properties == {} and result.notice


def test_flathubs_own_reference_file_installs_from_the_flathub_remote(tmp_path):
    assert launch.resolve(_ref(tmp_path, FLATHUB_REF), "/") == launch.Launch({"appSpec": "flathub:org.mozilla.firefox//stable"})
    assert launch.resolve(_ref(tmp_path, FLATHUB_REF, "APP.FLATPAKREF"), "/").properties == \
        {"appSpec": "flathub:org.mozilla.firefox//stable"}


def test_a_reference_to_another_repository_uses_it_only_if_it_is_already_configured(tmp_path):
    text = FLATHUB_REF.replace("https://dl.flathub.org/repo/", "https://Example.org/repo").replace("flathub", "elsewhere")
    known = launch.resolve(_ref(tmp_path, text), "/", lambda: {"https://example.org/repo": "mine"})
    assert known.properties == {"appSpec": "mine:org.mozilla.firefox//stable"}
    unknown = launch.resolve(_ref(tmp_path, text), "/", lambda: {"https://other.example/repo": "other"})
    assert unknown.properties == {} and "never adds a repository" in unknown.notice and "example.org" in unknown.notice.lower()


@pytest.mark.parametrize("change, expected", [
    (("Name=org.mozilla.firefox", "Name=../../evil"), "valid application"),
    (("Name=org.mozilla.firefox", "Name=org.x.App; rm"), "valid application"),
    (("Branch=stable", "Branch=--evil/../x"), "valid application"),
    (("IsRuntime=false", "IsRuntime=true"), "runtime"),
])
def test_a_reference_naming_something_invalid_opens_nothing(tmp_path, change, expected):
    result = launch.resolve(_ref(tmp_path, FLATHUB_REF.replace(*change)), "/")
    assert result.properties == {} and expected in result.notice


def test_a_reference_without_a_branch_means_stable(tmp_path):
    text = "\n".join(l for l in FLATHUB_REF.splitlines() if not l.startswith("Branch"))
    assert launch.resolve(_ref(tmp_path, text), "/").properties == {"appSpec": "flathub:org.mozilla.firefox//stable"}


@pytest.mark.parametrize("text, expected", [("x" * 70_000, "far larger"), ("[Other]\nName=x\n", "not a Flatpak reference"),
                                            ("\xff\xfe\x00 garbage", "not readable"), ("", "not a Flatpak reference")])
def test_a_reference_that_is_oversized_or_not_a_reference_opens_nothing(tmp_path, text, expected):
    path = tmp_path / "x.flatpakref"
    path.write_bytes(text.encode("latin-1", "replace") if text.startswith("\xff") else text.encode())
    result = launch.resolve(str(path), "/")
    assert result.properties == {} and expected in result.notice


def test_a_pipe_named_like_a_reference_never_blocks_the_window(tmp_path):
    fifo = tmp_path / "x.flatpakref"
    os.mkfifo(fifo)
    result = launch.resolve(str(fifo), "/")
    assert result.properties == {} and result.notice


def test_a_repository_file_is_explained_never_added(tmp_path):
    result = launch.resolve(_ref(tmp_path, "[Flatpak Repo]\nUrl=https://x.example/repo\n", "x.flatpakrepo"), "/")
    assert result.properties == {} and "never adds a repository" in result.notice


def test_other_files_open_the_install_page_for_analysis(tmp_path):
    deb = tmp_path / "my app.deb"
    deb.write_bytes(b"x")
    assert launch.resolve(str(deb), "/") == launch.Launch({"fileUrl": deb.as_uri()})
    assert launch.resolve(deb.as_uri(), "/").properties == {"fileUrl": deb.as_uri()}  # a browser passes file:// links
    assert launch.resolve("my app.deb", str(tmp_path)).properties == {"fileUrl": deb.as_uri()}  # relative to where it was started


@pytest.mark.parametrize("arg", ["https://evil.example/x.deb", "ftp://x/y", "file://otherhost/etc/x.deb", "javascript:alert(1)"])
def test_addresses_that_are_not_local_files_open_nothing(arg):
    result = launch.resolve(arg, "/")
    assert result.properties == {} and result.notice


def test_the_configured_remotes_are_listed_without_adding_anything():
    assert isinstance(service.configured_flatpak_remotes(), dict)


def test_a_repository_that_differs_only_in_the_case_of_its_path_is_a_different_repository(tmp_path):
    text = FLATHUB_REF.replace("https://dl.flathub.org/repo/", "https://example.org/Repo").replace("flathub", "elsewhere")
    result = launch.resolve(_ref(tmp_path, text), "/", lambda: {"https://example.org/repo": "mine"})
    assert result.properties == {} and "never adds a repository" in result.notice
    assert launch.normal_url("HTTPS://Example.ORG/Repo/") == "https://example.org/Repo"
    assert launch.normal_url("https://example.org/repo") != launch.normal_url("https://example.org/Repo")


@pytest.mark.parametrize("arg", ["file:relative.AppImage", "file:", "file:///tmp/a%00b.flatpakref", "/tmp/a\0b.flatpakref",
                                 "file://[::1/x.deb"])
def test_odd_addresses_give_a_notice_instead_of_an_error(arg):
    result = launch.resolve(arg, "/")
    assert result.properties == {} and result.notice


def test_invisible_and_right_to_left_characters_are_not_echoed_back(tmp_path):
    text = FLATHUB_REF.replace("https://dl.flathub.org/repo/", "https://exa‮mple.org/\x1b[31mrepo​")
    result = launch.resolve(_ref(tmp_path, text), "/")
    assert result.properties == {} and "example.org/[31mrepo" in result.notice  # shown, minus the hidden characters
    assert "‮" not in result.notice and "\x1b" not in result.notice and "​" not in result.notice


# -- Flathub's Install button opens a flatpak+https: link ---------------------------------------------------------------
@pytest.mark.parametrize("arg", [
    "flatpak+https://dl.flathub.org/repo/appstream/com.obsproject.Studio.flatpakref",
    "FLATPAK+HTTPS://DL.FLATHUB.ORG/repo/appstream/com.obsproject.Studio.flatpakref",
    "  flatpak+https://dl.flathub.org/repo/appstream/com.obsproject.Studio.flatpakref\n"])
def test_the_install_button_on_flathub_opens_that_app_from_flathub_without_fetching_anything(arg, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("nothing may be fetched while handling the link"))
    assert launch.resolve(arg, "/") == launch.Launch({"appSpec": "flathub:com.obsproject.Studio"})


@pytest.mark.parametrize("arg", [
    "flatpak+https://evil.example/repo/appstream/com.obsproject.Studio.flatpakref",  # not Flathub
    "flatpak+https://dl.flathub.org.evil.example/repo/appstream/com.obsproject.Studio.flatpakref",
    "flatpak+https://dl.flathub.org@evil.example/repo/appstream/com.obsproject.Studio.flatpakref",
    "flatpak+https://dl.flathub.org/beta-repo/appstream/com.obsproject.Studio.flatpakref",  # another repository
    "flatpak+https://dl.flathub.org/repo/appstream/../../x.flatpakref",
    "flatpak+https://dl.flathub.org/repo/appstream/com.obsproject.Studio.flatpakref?x=1",
    "flatpak+https://dl.flathub.org/repo/appstream/com.obsproject.Studio.flatpakref/extra",
    "flatpak+https://dl.flathub.org/repo/appstream/notanid.flatpakref",
    "flatpak+https://dl.flathub.org/repo/appstream/com.x.App;rm.flatpakref",
    "flatpak+https://dl.flathub.org/repo/appstream/" + "a." * 200 + "b.flatpakref",
    "flatpak+https://dl.flathub.org/repo/appstream/com.x.App.flatpakref\x00",
    "flatpak+https:", "flatpak+https://"])
def test_any_other_flatpak_https_link_only_explains_and_opens_nothing(arg):
    result = launch.resolve(arg, "/")
    assert result.properties == {} and result.notice
    assert "only follows Flathub's own links" in result.notice or "did not understand" in result.notice


def test_the_notice_for_a_foreign_link_names_its_host_without_hidden_characters():
    result = launch.resolve("flatpak+https://exa‮mple.org/repo/appstream/x.y.flatpakref", "/")
    assert "example.org" in result.notice and "‮" not in result.notice


def test_cygnus_claims_the_scheme_its_menu_entry_and_the_switch_use():
    from pathlib import Path

    from cygnus.core import handlers

    entry = (Path(__file__).resolve().parent.parent / "data/applications/io.github.omranabdulaziz.Cygnus.desktop").read_text()
    claimed = next(line for line in entry.splitlines() if line.startswith("MimeType=")).removeprefix("MimeType=").split(";")
    assert "x-scheme-handler/flatpak+https" in claimed and "x-scheme-handler/flatpak+https" in handlers.MIME_TYPES
    assert set(handlers.MIME_TYPES) <= set(claimed)  # the switch can only set what the menu entry claims
