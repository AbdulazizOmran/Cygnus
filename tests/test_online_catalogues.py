"""The online catalogues' store: switched on unless the person switched them off, downloaded only through the one fetch function written for each,
kept as plain files in Cygnus's cache folder, and read back with every record checked again. Nothing here uses the internet."""

import json
import os
import time

import pytest

from cygnus.core import online_catalogues as oc
from cygnus.core import paths, preferences
from cygnus.core.errors import CygnusError

@pytest.fixture(autouse=True)
def only_the_hub(monkeypatch):
    """The generic behaviour (switches, stale copies, kept files) is tested on one catalogue; the real list of catalogues grows and must not change these."""
    hub = oc.BY_ID["appimagehub"]
    monkeypatch.setattr(oc, "CATALOGUES", (hub,))
    monkeypatch.setattr(oc, "BY_ID", {hub.id: hub})


REC = {"repo": "acme/tool", "name": "Tool", "summary": "Does things", "categories": ["Utility"], "developer": "Acme"}
ID = "appimagehub"


def fetch_of(*records):
    calls = []

    def fetch(progress):
        calls.append(1)
        progress("reading")
        return list(records)

    fetch.calls = calls
    return fetch


def test_a_catalogue_is_on_until_the_person_switches_it_off_and_that_is_remembered():
    assert oc.is_on(ID) is True
    oc.set_on(ID, False)
    assert oc.is_on(ID) is False and preferences.load()["catalogues_off"] == [ID]
    oc.set_on(ID, True)
    assert oc.is_on(ID) is True and "catalogues_off" not in preferences.load()


def test_switching_keeps_the_rest_of_the_preferences_and_ignores_junk_in_the_list():
    preferences.save({"dismissed": {"a": ["b"]}, "catalogues_off": ["nonsense", 5, ID, "nonsense"]})
    assert oc.is_on(ID) is False
    oc.set_on(ID, False)
    assert preferences.load()["catalogues_off"] == [ID] and preferences.load()["dismissed"] == {"a": ["b"]}
    preferences.save({"catalogues_off": "appimagehub"})
    assert oc.is_on(ID) is True  # not a list: not a switch


@pytest.mark.parametrize("bad", ["", "evil", "../x", None, 5])
def test_an_unknown_catalogue_cannot_be_switched_or_downloaded(bad):
    with pytest.raises(CygnusError, match="not one of the online catalogues"):
        oc.set_on(bad, False)
    with pytest.raises(CygnusError, match="not one of the online catalogues"):
        oc.refresh(bad, fetch=fetch_of(REC))


def test_a_download_is_kept_checked_and_listed_with_its_age():
    assert oc.stored(ID) is None and oc.records(ID) == [] and oc.stamps() == []
    assert oc.refresh(ID, fetch=fetch_of(REC), now=1000.0) == 1
    assert oc.stored(ID) == {"records": [REC], "fetched": 1000.0}
    assert oc.records(ID) == [REC] and len(oc.stamps()) == 1 and oc.stamps()[0][0] == ID
    row = oc.status(now=1000.0 + 60)[0]
    assert (row["id"], row["on"], row["count"], row["fetched"], row["stale"]) == (ID, True, 1, 1000.0, False) and row["label"] and row["what"] and row["size"]


def test_a_copy_older_than_a_week_is_stale_and_is_asked_for_again_only_while_the_catalogue_is_on():
    oc.decide(True)
    oc.refresh(ID, fetch=fetch_of(REC), now=1000.0)
    week = oc.STALE_SECONDS
    assert oc.needs_download(now=1000.0 + week) == [] and oc.needs_download(now=1000.0 + week + 1) == [ID]
    oc.set_on(ID, False)
    assert oc.needs_download(now=1000.0 + week + 1) == [] and oc.records(ID) == [] and oc.stamps() == []  # off: not listed, not downloaded
    assert oc.status(now=1000.0)[0]["on"] is False and oc.status(now=1000.0)[0]["count"] == 1  # but what is there is still counted


def test_a_catalogue_that_was_never_downloaded_needs_a_download_once_the_person_has_said_yes():
    assert oc.needs_download() == [] and oc.status()[0]["stale"] is True  # not asked yet: nothing is due
    oc.decide(True)
    assert oc.needs_download() == [ID] and oc.status()[0]["stale"] is True and oc.status()[0]["fetched"] == 0.0


def test_a_failed_or_empty_download_leaves_the_copy_that_was_there():
    oc.refresh(ID, fetch=fetch_of(REC), now=1.0)

    def broken(progress):
        raise CygnusError("no network")

    with pytest.raises(CygnusError, match="no network"):
        oc.refresh(ID, fetch=broken)
    with pytest.raises(CygnusError, match="listed no programs"):
        oc.refresh(ID, fetch=fetch_of())
    with pytest.raises(CygnusError, match="listed no programs"):
        oc.refresh(ID, fetch=fetch_of({"repo": "acme/..", "name": "x", "summary": "", "categories": [], "developer": ""}))  # nothing valid is nothing
    assert oc.stored(ID) == {"records": [REC], "fetched": 1.0}


def test_what_a_fetch_returns_is_checked_before_it_is_kept():
    oc.refresh(ID, fetch=fetch_of(REC, {**REC, "repo": "acme/..", "name": "bad"}, {**REC, "repo": "acme/two", "name": "N\x1b[0m"}))
    assert [r["repo"] for r in oc.stored(ID)["records"]] == ["acme/tool", "acme/two"] and "\x1b" not in oc.stored(ID)["records"][1]["name"]


def test_a_download_is_allowed_while_the_catalogue_is_off_because_the_person_asked_for_it():
    oc.set_on(ID, False)
    assert oc.refresh(ID, fetch=fetch_of(REC)) == 1


def _write(text):
    path = paths.cache_dir() / "catalogues" / f"{ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


@pytest.mark.parametrize("content", ["not json", "[]", "5", '{"schema": 2, "fetched": 1, "records": []}', '{"schema": 1, "records": []}',
                                     '{"schema": 1, "fetched": "yesterday", "records": []}', '{"schema": 1, "fetched": true, "records": []}', "", "[" * 100000,
                                     '{"schema": 1, "fetched": null, "records": []}'])
def test_a_cache_file_that_is_not_ours_is_nothing(content):
    _write(content)
    assert oc.stored(ID) is None and oc.records(ID) == [] and oc.status()[0]["count"] == 0 and oc.status()[0]["stale"] is True


def test_a_cache_file_that_is_a_link_a_folder_or_too_large_is_nothing(monkeypatch, tmp_path):
    path = _write(json.dumps({"schema": 1, "fetched": 1.0, "records": [REC]}))
    assert oc.stored(ID) is not None
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(elsewhere)
    assert oc.stored(ID) is None  # a link is never followed
    path.unlink()
    path.mkdir()
    assert oc.stored(ID) is None
    path.rmdir()
    _write(json.dumps({"schema": 1, "fetched": 1.0, "records": [REC]}))
    monkeypatch.setattr(oc, "MAX_CACHE_BYTES", 10)
    assert oc.stored(ID) is None


def test_a_fifo_in_place_of_the_cache_file_does_not_block_anything():
    path = paths.cache_dir() / "catalogues" / f"{ID}.json"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    started = time.monotonic()
    assert oc.stored(ID) is None and time.monotonic() - started < 2


def test_a_cache_folder_that_is_a_link_is_refused_and_nothing_is_written_through_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.cache_dir()).mkdir(parents=True)
    (paths.cache_dir() / "catalogues").symlink_to(outside)
    with pytest.raises(CygnusError, match="not a plain folder"):
        oc.refresh(ID, fetch=fetch_of(REC))
    assert list(outside.iterdir()) == []


def test_the_stamps_change_when_the_download_is_replaced_so_the_lists_are_built_again():
    oc.refresh(ID, fetch=fetch_of(REC), now=1.0)
    before = oc.stamps()
    time.sleep(0.01)
    oc.refresh(ID, fetch=fetch_of(REC, {**REC, "repo": "acme/two"}), now=2.0)
    assert oc.stamps() != before



def test_nothing_is_due_until_the_person_has_been_asked_and_a_no_switches_every_catalogue_off():
    assert oc.decided() is False and oc.needs_download() == []
    oc.decide(False)
    assert oc.decided() is True and oc.is_on(ID) is False and oc.needs_download() == []
    oc.set_on(ID, True)  # one by one, later
    assert oc.is_on(ID) is True and oc.needs_download() == [ID]


def test_a_yes_never_switches_back_on_what_was_switched_off_and_keeps_the_rest_of_the_preferences():
    """Found by review: a catalogue switched off before the question was answered was switched on again, and downloaded, by the yes."""
    preferences.save({"dismissed": {"a": ["b"]}, "catalogues_off": [ID]})
    oc.decide(True)
    assert oc.decided() is True and oc.is_on(ID) is False and preferences.load()["dismissed"] == {"a": ["b"]} and oc.needs_download() == []


@pytest.mark.parametrize("value", [False, True, None, 1, "yes", "appimagehub", [], {}, [5], ["other"], [None, "x"]])
def test_only_a_list_that_names_the_catalogues_asked_about_counts_as_having_been_asked(value):
    preferences.save({"catalogues_decided": value})
    assert oc.decided() is False


def test_the_answer_covers_the_catalogues_there_were_and_a_new_one_is_asked_about_again(monkeypatch):
    oc.decide(False)
    assert oc.decided() is True and preferences.load()["catalogues_decided"] == [ID]
    extra = oc.Catalogue("newone", "New one", "A new catalogue.", "1 MB", oc.BY_ID[ID].module)
    monkeypatch.setattr(oc, "CATALOGUES", (*oc.CATALOGUES, extra))
    monkeypatch.setattr(oc, "BY_ID", {**oc.BY_ID, "newone": extra})
    assert oc.decided() is False and oc.needs_download() == []  # nothing is downloaded for the old answer, and the question comes back
    oc.decide(True)
    assert oc.decided() is True and oc.is_on("newone") is True


def test_a_file_that_is_not_a_regular_file_is_never_read_even_when_it_opened(monkeypatch):
    """A device or a pipe that opened (the tests cannot make a device) must be refused by its type, before anything is read from it."""
    import stat as stat_module

    _write(json.dumps({"schema": 1, "fetched": 1.0, "records": [REC]}))
    real = os.fstat

    class Odd:
        st_mode = stat_module.S_IFCHR | 0o666
        st_size = 0

    monkeypatch.setattr(oc.os, "fstat", lambda fd: Odd() if fd > 2 else real(fd))
    monkeypatch.setattr(oc.os, "fdopen", lambda *a, **k: pytest.fail("a file that is not a regular file was read"))
    assert oc.stored(ID) is None


def test_a_later_answer_changes_only_the_catalogues_it_is_the_first_answer_for(monkeypatch):
    """A re-asked question (a catalogue added by a newer version) must not undo what the person set by hand for the ones they already answered for."""
    oc.decide(True)
    extra = oc.Catalogue("newone", "New one", "A new catalogue.", "1 MB", oc.BY_ID[ID].module)
    monkeypatch.setattr(oc, "CATALOGUES", (*oc.CATALOGUES, extra))
    monkeypatch.setattr(oc, "BY_ID", {**oc.BY_ID, "newone": extra})
    oc.set_on(ID, False)  # the person turned the first one off by hand
    assert oc.decided() is False
    oc.decide(True)
    assert oc.is_on(ID) is False and oc.is_on("newone") is True and oc.decided() is True  # a yes switches the new one on and leaves the first one off
    oc.set_on(ID, True)
    monkeypatch.setattr(oc, "CATALOGUES", (*oc.CATALOGUES, oc.Catalogue("third", "Third", "Another.", "1 MB", oc.BY_ID[ID].module)))
    monkeypatch.setattr(oc, "BY_ID", {**oc.BY_ID, "third": oc.CATALOGUES[-1]})
    assert oc.decided() is False
    oc.decide(False)
    assert oc.is_on("third") is False and oc.is_on(ID) is True and oc.is_on("newone") is True  # a no switches off only the new one


@pytest.mark.parametrize("stamp", ["1e999", "NaN", "Infinity", "-Infinity", "-1", "9e18", "1" + "0" * 400, "true", "null", '"1000"'])
def test_a_download_date_that_is_not_a_plain_past_time_means_there_is_no_usable_copy(stamp):
    _write('{"schema": 1, "fetched": %s, "records": [%s]}' % (stamp, json.dumps(REC)))
    assert oc.stored(ID) is None and oc.records(ID) == [] and oc.status()[0]["stale"] is True and oc.status()[0]["count"] == 0
    oc.decide(True)
    assert oc.needs_download() == [ID]  # and it is simply downloaded again


@pytest.mark.parametrize("value", [5, None, "appimagehub", {"appimagehub": 1}, True, [5, None]])
def test_switches_still_work_when_the_saved_list_of_switched_off_catalogues_is_not_a_list(value):
    preferences.save({"catalogues_off": value})
    oc.set_on(ID, False)
    assert oc.is_on(ID) is False
    preferences.save({"catalogues_off": value})
    oc.set_on(ID, True)
    assert oc.is_on(ID) is True
    preferences.save({"catalogues_off": value})
    oc.decide(False)
    assert oc.is_on(ID) is False
