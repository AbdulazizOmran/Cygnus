"""The "support the project" link: one address, everywhere it appears, and it is only ever opened by a click."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
URL = "https://azizomran.gumroad.com/l/jtudib"


def test_the_same_address_is_used_in_the_app_the_repository_the_readme_and_the_appstream_data():
    qml = (REPO / "cygnus/gui/qml/SettingsPage.qml").read_text()
    assert f'Qt.openUrlExternally("{URL}")' in qml
    funding = (REPO / ".github/FUNDING.yml").read_text()
    assert re.search(rf'^custom: \["{re.escape(URL)}"\]$', funding, re.M)
    assert f"]({URL})" in (REPO / "README.md").read_text()
    urls = {u.get("type"): u.text for u in ET.parse(REPO / "data/metainfo/io.github.omranabdulaziz.Cygnus.metainfo.xml").getroot().findall("url")}
    assert urls["donation"] == URL and urls["homepage"].startswith("https://github.com/AbdulazizOmran/")


def test_the_address_is_https_on_gumroad_and_has_no_tracking_or_personal_parts():
    assert re.fullmatch(r"https://[a-z0-9-]+\.gumroad\.com/l/[A-Za-z0-9_-]+", URL)  # nothing after the product name: no query, no identifiers


def test_the_support_page_is_opened_only_by_the_button_and_says_so_in_words():
    qml = (REPO / "cygnus/gui/qml/SettingsPage.qml").read_text()
    block = qml[qml.index('text: "Support Cygnus"') - 60:][:600]
    assert "onClicked: Qt.openUrlExternally(" in block and "Cygnus itself sends nothing" in block
    assert qml.count(URL) == 1  # not opened from anywhere else (no timer, no start-up call)
    assert "openUrlExternally" not in (REPO / "cygnus/gui/qml/Main.qml").read_text().replace("showPassiveNotification", "")
    assert "Support Cygnus" in (REPO / "README.md").read_text() and "sends nothing" in (REPO / "README.md").read_text()
