"""Translations of the window's text: which language is chosen, which file is trusted, that a compiled translation really changes what the window says,
and two guards: the template lists exactly the texts in the QML, and no screen text is left unwrapped."""

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cygnus.gui import i18n

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "cygnus/gui/i18n/cygnus.ts"


# -- which language ----------------------------------------------------------------------------------------------------------------------------
def test_the_languages_come_from_the_environment_first_then_the_desktop():
    assert i18n.wanted({"CYGNUS_LANGUAGE": "de, fr_CA"}, ["ja-JP"]) == ["de", "fr_CA"]
    assert i18n.wanted({}, ["de-DE", "de", "en-US"]) == ["de-DE", "de", "en-US"]
    assert i18n.wanted({"CYGNUS_LANGUAGE": ""}, ["pt-BR"]) == ["pt-BR"]  # set but empty: the desktop's


@pytest.mark.parametrize("junk", ["C", "POSIX", "c", "", "../../etc", "de/../x", "x" * 40, "d", "12", "de;rm -rf", "de\nfr"])
def test_something_that_is_not_a_language_tag_is_dropped(junk):
    assert i18n.wanted({"CYGNUS_LANGUAGE": junk}) == []


def test_the_tags_qt_lists_for_a_script_and_a_region_are_kept():
    assert i18n.wanted({}, ["zh-Hant-TW", "sr-Cyrl-RS", "pt-Latn-BR", "de-Latn-DE-1996"]) == ["zh-Hant-TW", "sr-Cyrl-RS", "pt-Latn-BR"]  # three parts at most


def test_a_locale_with_an_encoding_and_a_modifier_is_reduced_to_its_language_and_repeats_are_dropped():
    assert i18n.wanted({"CYGNUS_LANGUAGE": "de_DE.UTF-8,de_DE@euro,de_DE"}) == ["de_DE"]


@pytest.mark.parametrize("tag,names", [("de", ["cygnus_de.qm"]), ("de-DE", ["cygnus_de_DE.qm", "cygnus_de.qm"]), ("pt_BR", ["cygnus_pt_BR.qm", "cygnus_pt.qm"]),
                                       ("zh-Hans", ["cygnus_zh_Hans.qm", "cygnus_zh.qm"]),
                                       ("zh-Hant-TW", ["cygnus_zh_Hant_TW.qm", "cygnus_zh_Hant.qm", "cygnus_zh.qm"])])
def test_the_file_names_go_from_the_specific_to_the_general(tag, names):
    assert i18n.file_names(tag) == names


def test_the_folders_are_the_package_the_system_and_an_absolute_one_you_name():
    assert i18n.folders({"CYGNUS_TRANSLATIONS": "/opt/try"}) == [Path("/opt/try"), i18n.PACKAGE_FOLDER, i18n.SYSTEM_FOLDER]
    assert i18n.folders({"CYGNUS_TRANSLATIONS": "relative/dir"}) == [i18n.PACKAGE_FOLDER, i18n.SYSTEM_FOLDER]
    assert i18n.folders({}) == [i18n.PACKAGE_FOLDER, i18n.SYSTEM_FOLDER]


# -- which file is trusted ---------------------------------------------------------------------------------------------------------------------
def test_the_first_language_with_a_file_wins_and_the_specific_file_beats_the_general_one(tmp_path):
    (tmp_path / "cygnus_fr.qm").write_bytes(b"x")
    (tmp_path / "cygnus_de.qm").write_bytes(b"x")
    (tmp_path / "cygnus_de_AT.qm").write_bytes(b"x")
    assert i18n.find(["de-AT", "fr"], [tmp_path]).name == "cygnus_de_AT.qm"
    assert i18n.find(["de-CH", "fr"], [tmp_path]).name == "cygnus_de.qm"
    assert i18n.find(["ja", "fr"], [tmp_path]).name == "cygnus_fr.qm"
    assert i18n.find(["ja"], [tmp_path]) is None and i18n.find([], [tmp_path]) is None


def test_a_person_who_lists_english_first_gets_english_even_when_a_later_language_has_a_file(tmp_path):
    """KDE's list for LANGUAGE=en_US:de reaches Qt as en-US, en, de-DE, de: English is what the texts are written in, so nothing after it applies."""
    (tmp_path / "cygnus_de.qm").write_bytes(b"x")
    assert i18n.find(["en-US", "en", "de-DE", "de"], [tmp_path]) is None
    assert i18n.find_all(["en-GB", "de"], [tmp_path]) == []
    assert i18n.find(["de-DE", "de", "en"], [tmp_path]).name == "cygnus_de.qm"  # German first: German
    (tmp_path / "cygnus_en_GB.qm").write_bytes(b"x")  # a British-English translation, should one exist, is used
    assert i18n.find(["en-GB", "de"], [tmp_path]).name == "cygnus_en_GB.qm"


def test_a_regional_file_is_used_together_with_the_general_one_so_a_missing_text_falls_back(tmp_path):
    (tmp_path / "cygnus_de.qm").write_bytes(b"x")
    (tmp_path / "cygnus_de_AT.qm").write_bytes(b"x")
    assert [p.name for p in i18n.find_all(["de-AT"], [tmp_path])] == ["cygnus_de_AT.qm", "cygnus_de.qm"]  # most specific first
    assert [p.name for p in i18n.find_all(["de-AT", "fr"], [tmp_path, tmp_path])] == ["cygnus_de_AT.qm", "cygnus_de.qm"]


def test_an_earlier_folder_beats_a_later_one_for_the_same_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for folder in (a, b):
        folder.mkdir()
        (folder / "cygnus_de.qm").write_bytes(b"x")
    assert i18n.find(["de"], [a, b]).parent == a


def test_a_link_a_folder_an_empty_file_and_a_huge_one_are_never_loaded(tmp_path, monkeypatch):
    real = tmp_path / "real.qm"
    real.write_bytes(b"x")
    (tmp_path / "cygnus_de.qm").symlink_to(real)
    (tmp_path / "cygnus_fr.qm").mkdir()
    (tmp_path / "cygnus_es.qm").write_bytes(b"")
    (tmp_path / "cygnus_it.qm").write_bytes(b"x" * 20)
    monkeypatch.setattr(i18n, "MAX_BYTES", 10)
    assert i18n.find(["de", "fr", "es", "it"], [tmp_path]) is None


# -- a real translation in a real window ------------------------------------------------------------------------------------------------------
needs_qt_tools = pytest.mark.needs_tool("lrelease6", "lupdate6")


def _compile(folder: Path, language: str, pairs: dict[tuple[str, str], str]) -> Path:
    contexts: dict[str, list[tuple[str, str]]] = {}
    for (context, source), translation in pairs.items():
        contexts.setdefault(context, []).append((source, translation))
    body = "".join(f"<context><name>{c}</name>" + "".join(f"<message><source>{s}</source><translation>{t}</translation></message>" for s, t in items)
                   + "</context>" for c, items in contexts.items())
    ts = folder / f"cygnus_{language}.ts"
    ts.write_text(f'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE TS><TS version="2.1" language="{language}">{body}</TS>')
    subprocess.run(["lrelease6", "-silent", str(ts), "-qm", str(folder / f"cygnus_{language}.qm")], check=True, capture_output=True)
    return folder / f"cygnus_{language}.qm"


def _run_window(tmp_path, language, folder):
    env = {**os.environ, **{var: str(tmp_path / sub) for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                                                                       ("XDG_STATE_HOME", "state"))}, "CYGNUS_LANGUAGE": language, "CYGNUS_TRANSLATIONS": str(folder)}
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name("gui_i18n_drive.py"))], capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.stdout + proc.stderr)[-2000:]
    return json.loads(lines[-1])


pytest.importorskip("PySide6")


@needs_qt_tools
def test_a_compiled_translation_changes_what_the_window_says_and_everything_else_stays_english(tmp_path):
    folder = tmp_path / "tr"
    folder.mkdir()
    _compile(folder, "de", {("Main", "Discover"): "Entdecken", ("Main", "Updates"): "Aktualisierungen"})
    result = _run_window(tmp_path, "de", folder)
    assert result["used"] == "cygnus_de.qm"
    assert result["drawer"] == ["Applications", "Install", "Entdecken", "Packages", "Aktualisierungen", "Storage", "Settings"]  # translated where there is a translation


@needs_qt_tools
def test_a_regional_translation_falls_back_to_the_general_one_for_texts_it_lacks(tmp_path):
    folder = tmp_path / "tr"
    folder.mkdir()
    _compile(folder, "de", {("Main", "Discover"): "Entdecken", ("Main", "Updates"): "Aktualisierungen"})
    _compile(folder, "de_AT", {("Main", "Updates"): "Updates (AT)"})  # the regional file lacks "Discover"
    result = _run_window(tmp_path, "de-AT", folder)
    assert result["drawer"][2] == "Entdecken" and result["drawer"][4] == "Updates (AT)"
    assert result["used"] == "cygnus_de_AT.qm"


@needs_qt_tools
def test_a_language_without_a_translation_leaves_the_window_in_english(tmp_path):
    folder = tmp_path / "tr"
    folder.mkdir()
    _compile(folder, "de", {("Main", "Discover"): "Entdecken"})
    result = _run_window(tmp_path, "ja", folder)
    assert result["used"] is None and result["drawer"][2] == "Discover"


@needs_qt_tools
def test_a_damaged_translation_file_is_ignored_not_a_crash(tmp_path):
    folder = tmp_path / "tr"
    folder.mkdir()
    (folder / "cygnus_de.qm").write_bytes(b"this is not a qm file at all" * 4)
    result = _run_window(tmp_path, "de", folder)
    assert result["used"] is None and result["drawer"][2] == "Discover"


def test_the_program_installs_the_translation_before_it_builds_the_window():
    """The window tests start the loader themselves; this keeps the program's own start-up in step with them."""
    source = (ROOT / "cygnus/gui/app.py").read_text()
    assert "i18n.install(app)" in source
    assert source.index("i18n.install(app)") < source.index("QQmlApplicationEngine(")
    assert source.index("app = QGuiApplication(argv)") < source.index("i18n.install(app)")


# -- the guards -----------------------------------------------------------------------------------------------------------------------------------
def test_the_template_is_the_template_it_says_it_is():
    root = ET.parse(TEMPLATE).getroot()
    assert root.tag == "TS" and root.get("sourcelanguage") == "en"
    messages = root.findall(".//message")
    assert len(messages) > 250
    assert all((m.find("translation").text or "").strip() == "" and not any((f.text or "").strip() for f in m.findall(".//numerusform")) for m in messages)  # no translation, not even a plural form: only the texts


@pytest.mark.needs_tool("lupdate6")
def test_the_template_lists_exactly_the_texts_in_the_screens(tmp_path):
    """Add a qsTr() or change a text and forget the template, and this fails: run packaging/translations.sh update."""
    fresh = tmp_path / "fresh.ts"
    subprocess.run(["lupdate6", "-no-obsolete", "-extensions", "qml", "-source-language", "en", str(ROOT / "cygnus/gui/qml"), "-ts", str(fresh)], check=True,
                   capture_output=True)

    def sources(path):
        return sorted((c.findtext("name"), m.findtext("source")) for c in ET.parse(path).getroot().findall("context") for m in c.findall("message"))
    assert sources(fresh) == sources(TEMPLATE)


ENGLISH_ONLY_AT_MOST = 180  # the texts that read like text for a person and are not translatable yet; it may go down, never up


def _coverage():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import translation_coverage as tc
    finally:
        sys.path.pop(0)
    return tc


def test_no_new_text_is_written_on_a_screen_that_a_translator_cannot_reach():
    """A text added as a plain string (in a property, a ternary, a message) rather than qsTr() raises the English-only count and fails this: wrap it, or if it
    is built from pieces make it one sentence with %1 markers (root.fill(qsTr("... %1 ..."), [value])), then run packaging/translations.sh update."""
    result = _coverage().scan(ROOT / "cygnus/gui/qml")
    english = sum(c["english"] for c in result.values())
    assert english <= ENGLISH_ONLY_AT_MOST, {name: c["english"] for name, c in result.items() if c["english"]}
    assert sum(c["translatable"] for c in result.values()) > 380


def test_the_coverage_script_tells_translatable_from_english_only_from_not_text():
    tc = _coverage()
    scan = tc.scan_text
    assert scan('text: qsTr("Hello there")') == {"translatable": 1, "english": 0}
    assert scan('text: "Hello there"') == {"translatable": 0, "english": 1}
    assert scan('text: "Went back to " + name + "."') == {"translatable": 0, "english": 1}  # a piece of a sentence built at run time cannot be translated
    assert scan('objectName: "browser dialog"; source: "page.qml"; x: "org.kde.kirigami"; u: "https://example.org/a b"') == {"translatable": 0, "english": 0}
    assert scan('state === "installed"; id: "flatpak"; kind: "x-scheme-handler/http"') == {"translatable": 0, "english": 0}
    assert scan('// text: "A comment that says things"\n/* "and a block comment" */') == {"translatable": 0, "english": 0}
    assert scan('text: "Waiting…"') == {"translatable": 0, "english": 1}  # a word ending in … reads like text
    assert tc.display_like("Up to date") and tc.display_like("Done.") and not tc.display_like("installed") and not tc.display_like("a/b/c.png")


def test_the_identifiers_a_person_could_read_are_not_translatable():
    qml = (ROOT / "cygnus/gui/qml/SettingsPage.qml").read_text()
    assert 'qsTr("io.github' not in qml and "description: root.appId" in qml  # an identifier is not text to translate
    assert "readonly property string appId" in (ROOT / "cygnus/gui/qml/Main.qml").read_text()


def test_a_value_that_contains_a_marker_is_not_filled_in_twice():
    """qsTr("%1 ... %2").arg(a).arg(b) re-scans a: a mount point called "/mnt/%2" came out as "/mnt/<second value>". root.fill does it in one pass."""
    qml = (ROOT / "cygnus/gui/qml/Main.qml").read_text()
    body = qml[qml.index("function fill(template, values)"):][:260]
    assert "replace(/%(\\d+)/g" in body.replace("\\\\", "\\")
    for page in ("StoragePage.qml", "DiscoverAppPage.qml"):
        text = (ROOT / "cygnus/gui/qml" / page).read_text()
        assert ").arg(" not in "".join(l for l in text.splitlines() if "%2" in l), page  # more than one marker: root.fill, never a chain of arg()
    assert "root.fill(qsTr(" in (ROOT / "cygnus/gui/qml/StoragePage.qml").read_text() and "root.fill(qsTr(" in (ROOT / "cygnus/gui/qml/DiscoverAppPage.qml").read_text()


# -- the script that starts a translation -----------------------------------------------------------------------------------------------------------
def _script_copy(tmp_path):
    work = tmp_path / "work"
    (work / "packaging").mkdir(parents=True)
    shutil.copy(ROOT / "packaging/translations.sh", work / "packaging/translations.sh")
    (work / "packaging/translation_coverage.py").write_text("print('coverage')\n")
    shutil.copytree(ROOT / "cygnus/gui/qml", work / "cygnus/gui/qml")
    (work / "cygnus/gui/i18n").mkdir(parents=True)
    shutil.copy(TEMPLATE, work / "cygnus/gui/i18n/cygnus.ts")
    return work


@needs_qt_tools
def test_starting_a_translation_gives_the_language_and_the_right_plural_forms_and_a_hyphenated_tag_is_made_loadable(tmp_path):
    work = _script_copy(tmp_path)
    done = subprocess.run(["sh", "packaging/translations.sh", "merge", "pt-BR"], cwd=work, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    ts = work / "cygnus/gui/i18n/cygnus_pt_BR.ts"  # the underscore spelling is the one the loader looks for
    assert ts.exists() and not (work / "cygnus/gui/i18n/cygnus_pt-BR.ts").exists()
    root = ET.parse(ts).getroot()
    assert root.get("language") == "pt_BR" and root.get("sourcelanguage") == "en"
    plural = next(m for m in root.iter("message") if m.get("numerus") == "yes")
    assert len(plural.findall(".//numerusform")) == 2  # Portuguese has two forms, so the translator is asked for two
    built = subprocess.run(["sh", "packaging/translations.sh", "build"], cwd=work, capture_output=True, text=True)
    assert built.returncode == 0 and (work / "cygnus/gui/i18n/cygnus_pt_BR.qm").exists()
    assert i18n.find(["pt-BR"], [work / "cygnus/gui/i18n"]).name == "cygnus_pt_BR.qm"


@needs_qt_tools
@pytest.mark.parametrize("bad", ["../x", "", "d", "de/../x", "de;rm", "de fr", "-x", "toolong4"])
def test_a_language_that_is_not_a_tag_is_refused_and_nothing_is_written(tmp_path, bad):
    work = _script_copy(tmp_path)
    done = subprocess.run(["sh", "packaging/translations.sh", "merge", bad], cwd=work, capture_output=True, text=True)
    assert done.returncode == 2 and "usage" in done.stdout
    assert sorted(p.name for p in (work / "cygnus/gui/i18n").iterdir()) == ["cygnus.ts"]


def test_building_with_no_translations_says_so_and_succeeds(tmp_path):
    work = _script_copy(tmp_path)
    done = subprocess.run(["sh", "packaging/translations.sh", "build"], cwd=work, capture_output=True, text=True)
    assert done.returncode == 0 and "no translations yet" in done.stdout


def test_building_a_translation_without_the_qt_tool_says_what_to_install(tmp_path):
    work = _script_copy(tmp_path)
    (work / "cygnus/gui/i18n/cygnus_de.ts").write_text("<TS/>")
    bin_dir = tmp_path / "bin-without-qt"
    bin_dir.mkdir()
    for tool in ("dirname", "tr"):  # what the script itself needs, and nothing else: no lrelease6 on this path
        (bin_dir / tool).symlink_to(shutil.which(tool))
    env = {**os.environ, "PATH": str(bin_dir)}
    sh = shutil.which("sh")
    done = subprocess.run([sh, "packaging/translations.sh", "build"], cwd=work, capture_output=True, text=True, env=env)
    assert done.returncode == 1 and "lrelease6 is not installed (package qt6-tools)" in done.stdout
