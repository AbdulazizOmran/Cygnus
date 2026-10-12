"""Translations of the window's text.

The text of the screens is written with `qsTr("…")`, so a translation file can replace it. A translation is a compiled Qt file called
`cygnus_<language>.qm` (for example `cygnus_de.qm` or `cygnus_pt_BR.qm`), made from a `.ts` file that a translator writes (see docs/translating.md);
Cygnus ships a template (`cygnus.ts`) with every text that can be translated, and no translation of its own: nothing is translated by a machine.

Which one is used: the language(s) of your desktop, or the ones named in CYGNUS_LANGUAGE (for example `CYGNUS_LANGUAGE=de`) when it is set. Files are
looked for next to this module (where a package puts them), in /usr/share/cygnus/translations, and in the folder named by CYGNUS_TRANSLATIONS when
it is set (to try a translation before it is installed). Only regular files of a sane size are loaded, and a language with no file simply stays English.

Not everything can be translated this way: the texts the program builds in Python (error messages, what a plan will do, notes about a package,
the terminal) are in English, and so is any text built from several pieces on a screen that has not been converted yet. docs/translating.md says
how much of the screen text is covered."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

PACKAGE_FOLDER = Path(__file__).with_name("i18n")
SYSTEM_FOLDER = Path("/usr/share/cygnus/translations")
MAX_BYTES = 8 * 1024 * 1024
_TAG = re.compile(r"[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8}){0,2}\Z")  # a language tag as it appears in file names: de, pt_BR, zh-Hans, zh-Hant-TW


def wanted(environ: dict[str, str] | None = None, system: Iterable[str] = ()) -> list[str]:
    """The languages to try, best first: those in CYGNUS_LANGUAGE (comma-separated), else the desktop's. Anything that is not a language tag is dropped."""
    environ = os.environ if environ is None else environ
    named = [x.strip() for x in environ.get("CYGNUS_LANGUAGE", "").split(",") if x.strip()]
    tags = named or [str(x) for x in system]
    out: list[str] = []
    for tag in tags:
        tag = tag.split(".")[0].split("@")[0]  # "de_DE.UTF-8" -> "de_DE"
        if tag and tag.lower() not in ("c", "posix") and _TAG.match(tag) and tag not in out:
            out.append(tag)
    return out


def file_names(tag: str) -> list[str]:
    """The files a language may be in, most specific first: de-DE -> cygnus_de_DE.qm, cygnus_de.qm; zh-Hant-TW -> cygnus_zh_Hant_TW.qm,
    cygnus_zh_Hant.qm, cygnus_zh.qm."""
    parts = re.split(r"[_-]", tag)
    return [f"cygnus_{'_'.join(parts[:n])}.qm" for n in range(len(parts), 0, -1)]


def folders(environ: dict[str, str] | None = None) -> list[Path]:
    environ = os.environ if environ is None else environ
    extra = environ.get("CYGNUS_TRANSLATIONS", "")
    return ([Path(extra)] if extra and os.path.isabs(extra) else []) + [PACKAGE_FOLDER, SYSTEM_FOLDER]


def _usable(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_BYTES


def find_all(languages: Iterable[str], search: Iterable[Path]) -> list[Path]:
    """The translation files for the first language that has any, most specific first: regular files (not links) of a sane size, at most one
    per name (an earlier folder wins). English is the language the texts are written in, so a list that reaches an English entry before a
    language that has a file stops there: a person who lists English first gets English."""
    search = list(search)
    for tag in languages:
        found = []
        for name in file_names(tag):
            path = next((folder / name for folder in search if _usable(folder / name)), None)
            if path is not None:
                found.append(path)
        if found:
            return found
        if re.split(r"[_-]", tag)[0].lower() == "en":
            return []
    return []


def find(languages: Iterable[str], search: Iterable[Path]) -> Path | None:
    """The most specific translation file `find_all` finds, or None."""
    found = find_all(languages, search)
    return found[0] if found else None


def install(app, *, environ: dict[str, str] | None = None, translator_class=None) -> Path | None:
    """Load the translation for this desktop's language into `app` (a QGuiApplication). -> the file used, or None (English)."""
    from PySide6.QtCore import QLocale, QTranslator

    files = find_all(wanted(environ, QLocale.system().uiLanguages()), folders(environ))
    loaded: Path | None = None  # the most specific file that loaded
    for path in reversed(files):  # the general file first, the regional one last: Qt asks the translator installed last first, so a text the
        translator = (translator_class or QTranslator)(app)  # regional file lacks still comes from the general one
        if translator.load(str(path)) and app.installTranslator(translator):
            loaded = path
    return loaded
