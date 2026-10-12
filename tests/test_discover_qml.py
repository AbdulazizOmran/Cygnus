"""Rules the Discover screens (and every page's messages) must keep, read from the QML itself: text that reaches a message is made safe, a message
that can be closed does not depend on a binding closing would remove, and "Show more" continues the list that is on screen."""

import re
from pathlib import Path

import pytest

QML = Path(__file__).resolve().parent.parent / "cygnus/gui/qml"
PAGES = ("DiscoverPage.qml", "DiscoverAppPage.qml")


def _inline_messages(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*Kirigami\.InlineMessage\s*\{\s*$", line):
            depth, j, body = 1, i + 1, []
            while j < len(lines) and depth:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth >= 1:
                    body.append(lines[j])
                j += 1
            yield i + 1, "\n".join(body)


@pytest.mark.parametrize("name", PAGES)
def test_every_error_text_is_made_safe_before_it_reaches_a_message(name):
    """A message reads markup: what goes into errorText (error text from the backend, names from data files) goes through root.plain()."""
    code = "\n".join(re.sub(r"\s//(?![/:]).*$", "", l) for l in (QML / name).read_text().splitlines())
    assignments = re.findall(r"errorText = (.+?);", code)
    assert assignments
    assert all(a == '""' or a.startswith("root.plain(") for a in assignments), assignments


def test_the_list_of_unreadable_data_files_is_made_safe_before_it_is_shown():
    code = (QML / "DiscoverPage.qml").read_text()
    block = code[code.index("Some application lists could not be read"):][:200]
    assert "root.plain(" in code[code.index("Some application lists could not be read") - 120:code.index("Some application lists could not be read")]
    assert "problems.join" in code[code.index("Some application lists could not be read"):][:300] or "problems.join" in block


def test_a_message_that_can_be_closed_is_never_bound_to_a_condition():
    """Closing a Kirigami message sets its visible to false, which removes a binding: the next error would never show. A message with a close
    button therefore sets visible by hand (visible: false to start with), as the pages that have one do. (Checked on the Discover pages, whose
    errors come again and again; the Install page's one-time notice is set once when the page is made.)"""
    offenders = []
    for f in sorted(QML / name for name in PAGES):
        for line_no, body in _inline_messages(f.read_text()):
            closable = re.search(r"showCloseButton:\s*true", body)
            bound = re.search(r"^\s*visible:\s*(?!false\b)(?!true\b)\S", body, re.M)
            if closable and bound:
                offenders.append(f"{f.name}:{line_no}")
    assert offenders == []


def test_show_more_continues_what_was_asked_not_what_is_typed_since():
    code = (QML / "DiscoverPage.qml").read_text()
    search = code[code.index("function search(more)"):code.index("function load()")]
    assert "query: resultQuery" in search and "group: resultGroup" in search and "kind: resultKind" in search
    assert "backend.discoverSearch(asked.query, asked.group, asked.kind, offset)" in search
    assert "resultQuery = asked.query" in search  # remembered only when the answer is the one still wanted (after the sequence check)
    assert search.index("if (seq !== searchSeq)") < search.index("resultQuery = asked.query")
