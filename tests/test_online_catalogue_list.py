"""The real list of online catalogues (the other test file narrows it to one on purpose): each has a module that can fetch and check its records."""

from cygnus.core import online_catalogues as oc


def test_the_real_list_of_catalogues_has_a_module_that_can_fetch_and_check_for_each():
    assert [c.id for c in oc.CATALOGUES] == ["appimagehub", "debian", "fedora"] and set(oc.BY_ID) == {c.id for c in oc.CATALOGUES}
    for cat in oc.CATALOGUES:
        assert callable(cat.module.fetch) and callable(cat.module.clean) and cat.module.ID == cat.id
        assert cat.label and cat.what.endswith(".") and cat.size


def test_every_catalogue_is_on_and_not_yet_downloaded_on_a_fresh_account():
    rows = oc.status()
    assert [(r["id"], r["on"], r["count"], r["stale"]) for r in rows] == [("appimagehub", True, 0, True), ("debian", True, 0, True), ("fedora", True, 0, True)]
    assert oc.decided() is False and oc.needs_download() == [] and oc.stamps() == []  # nothing is downloaded by itself before the person has been asked
    oc.decide(True)
    assert oc.needs_download() == ["appimagehub", "debian", "fedora"]
