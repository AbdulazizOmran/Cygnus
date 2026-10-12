#!/usr/bin/env python3
"""How much of the window's text can be translated, counted honestly: every string literal in the QML that reads like text for a person (two or more
words, or a word ending in … : . ! ?) is either inside qsTr() ("translatable") or not ("English only"). Pieces of a sentence put together at run time
with + are counted as English only, because a translator cannot reach a half sentence. The unit is the string literal, not the sentence: a sentence
written in three pieces counts as three, and the percentage says how many of those pieces a translator can reach. Strings that are plainly identifiers,
addresses or codes are not counted, and a heuristic decides which strings those are, so the figure is an honest estimate and not an exact census. Run from the repository root: python3 packaging/translation_coverage.py   (tests/test_i18n.py keeps the English-only count from growing)"""
import re
import sys
from pathlib import Path

_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def display_like(text: str) -> bool:
    """Does this string read like text a person is shown (rather than a code, a path, an address, a property name)?"""
    stripped = text.strip()
    if not re.search(r"[A-Za-z]{2,}", stripped) or re.fullmatch(r"[\w./:#@%+=~-]+", stripped) and not stripped.endswith(("…", ".", ":", "!", "?")):
        return False
    if stripped.startswith(("http://", "https://", "/", "file:", "qrc:", "image://")) or re.fullmatch(r"[\w.-]+/[\w./-]+", stripped):
        return False
    return " " in stripped or stripped.endswith(("…", ".", ":", "!", "?"))


def classify(text: str, before: str, after: str) -> str | None:
    """"translatable" (inside qsTr), "english" (display-like and not), or None (not text for a person)."""
    if before.rstrip().endswith("qsTr(") or re.search(r"qsTr\(\s*$", before):
        return "translatable"
    if not display_like(text):
        return None
    if re.search(r"(objectName|import|console\.\w+|openUrlExternally|\.indexOf|\.startsWith|\.endsWith|\.split|\.join|\.test|\.match)\s*[:(]?\s*$", before):
        return None
    return "english"


def scan_text(source: str) -> dict[str, int]:
    counts = {"translatable": 0, "english": 0}
    in_block = False
    for line in source.splitlines():
        code = line
        if in_block:
            if "*/" not in code:
                continue
            code, in_block = code.split("*/", 1)[1], False
        code = re.sub(r"/\*.*?\*/", "", code)
        if "/*" in code:
            code, in_block = code.split("/*", 1)[0], True
        code = re.sub(r"(?<![:\"'\\])//.*$", "", code)  # a comment ("//" not inside a string or after "http:")
        for m in _STRING.finditer(code):
            kind = classify(m.group(1), code[:m.start()], code[m.end():])
            if kind:
                counts[kind] += 1
    return counts


def scan(folder: Path) -> dict[str, dict[str, int]]:
    return {path.name: scan_text(path.read_text()) for path in sorted(folder.glob("*.qml"))}


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "cygnus/gui/qml"
    result = scan(folder)
    total = {"translatable": 0, "english": 0}
    for name, counts in result.items():
        print(f"{name:22} translatable {counts['translatable']:4}   English only {counts['english']:4}")
        for k in total:
            total[k] += counts[k]
    done, left = total["translatable"], total["english"]
    print(f"\nTOTAL translatable {done}, English only {left} -> {100 * done / max(1, done + left):.0f}% of the texts that read like text for a person")
