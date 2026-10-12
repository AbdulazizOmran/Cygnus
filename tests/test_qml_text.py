"""Text from packages, manifests, repositories or commands is never rendered as markup in the GUI."""

import re
from pathlib import Path

QML = Path(__file__).resolve().parent.parent / "cygnus/gui/qml"


def _blocks(text: str, kinds: tuple[str, ...]):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        # "QQC2.Label {" on its own line, or after a property name ("delegate: QQC2.Label {")
        if re.match(r"^\s*(?:\w+:\s*)?(" + "|".join(map(re.escape, kinds)) + r")\s*\{\s*$", line):
            depth, j, body = 1, i + 1, []
            while j < len(lines) and depth:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 1:
                    body.append(lines[j])
                j += 1
            yield i + 1, body


def test_every_label_heading_and_text_area_is_plain_text():
    missing = [f"{f.name}:{n}" for f in QML.glob("*.qml")
               for n, body in _blocks(f.read_text(), ("QQC2.Label", "Kirigami.Heading", "QQC2.TextArea"))
               if not any(re.search(r"textFormat:\s*(Text|TextEdit)\.PlainText\b", l) for l in body)]  # the value, not just the word
    assert missing == []


def test_markup_messages_escape_what_they_insert():
    offenders = []
    for f in QML.glob("*.qml"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r'text: .*"<(b|i|br)>', line):
                # every inserted expression must go through root.plain(); literal text and numbers are fine
                inserted = re.findall(r'\+ (?!root\.plain\()(?!")([A-Za-z_][\w.\[\]]*)', line)
                inserted = [x for x in inserted if not x.endswith(("minutes", ".length"))]
                if inserted:
                    offenders.append(f"{f.name}:{n}: {inserted}")
    assert offenders == []


def test_pages_ignore_answers_that_arrive_after_they_were_closed():
    """Pages ask the backend through page.ask(), which drops late answers. A destroyed page's callbacks cannot
    be relied on, so nothing that changes the registry may be left to one: the backend does it on commit."""
    pages = list(QML.glob("*Page.qml"))
    assert len(pages) == 10  # the two Packages pages and the two Discover pages are in
    direct = {}
    for f in pages:
        text = f.read_text()
        assert "Component.onDestruction: life.gone = true" in text, f.name
        direct[f.name] = len(re.findall(r"root\.request\(", text))
        for slot in ("recordAurInstall", "recordAurDependency", "recordPackageInstall", "forgetInstallation"):
            assert slot not in text, f"{f.name} must not record state from a callback ({slot})"
    assert set(direct.values()) == {1}   # only each page's own ask()
    for f in pages:  # and that ask() really drops an answer for a page that is gone (a comment or an unconditional call does not)
        code = "\n".join(re.sub(r"\s//(?![/:]).*$", "", line) for line in f.read_text().splitlines())
        assert re.search(r"if \(!alive\.gone\)\s+callback\(r\);\s+else\s+root\.lateAnswer\(what, r\);", code), f.name


def test_the_install_page_stays_consistent_while_an_install_runs():
    """Round 4 (gui 3, 4, 9): no stale success banner, no new input accepted mid-install, and the AUR path
    stays busy while its dependencies are planned."""
    text = (QML / "InstallPage.qml").read_text()
    analyse = text[text.index("    function analyse()"):text.index("    // AUR: dependencies from your repositories (password) → build as you")]
    install = text[text.index("    function install()"):text.index("    Kirigami.PromptDialog {\n        id: confirmDialog")]
    assert "doneMessage.visible = false" in analyse and "doneMessage.visible = false" in install
    aur_branch = install[install.index('plan.kind === "aur"'):install.index('plan.format === "appimage"')]
    assert "installing = false" not in aur_branch  # the page stays busy until the confirmation is open
    assert "if (page.installing)" in text[text.index("id: fileDialog"):text.index("DropArea")]
    assert "!page.installing" in text[text.index("DropArea"):text.index("ColumnLayout")]
    assert "!page.installing" in text[text.index("id: lookUp"):text.index("id: lookUp") + 400]


def test_dialog_titles_built_from_names_are_flattened():
    """Round 4 (gui 7): a name from AppImage metadata must never become markup in a dialog title."""
    for name in ("AppPage.qml", "StoragePage.qml"):
        text = (QML / name).read_text()
        for m in re.finditer(r"^\s*title:\s*(.+)$", text, re.M):
            expr = m.group(1)
            if "+" in expr and "page." in expr:  # a title assembled from page data
                assert expr.startswith("root.flat("), f"{name}: {expr}"


def _function(text: str, signature: str) -> str:
    """The body of a QML function (from its signature to the closing brace at the same indentation)."""
    start = text.index(signature)
    indent = re.match(r" *", signature).group(0)
    end = text.index("\n" + indent + "}", start)
    return text[start:end]


def test_the_uninstall_question_never_opens_without_knowing_what_is_in_the_folder_and_never_carries_a_tick_over():
    """Round 12: the tick for "also delete the folder" starts clear for every program, an archive program's folder is looked at before
    the question is put (and a failure to look stops it), and only a box that is shown and ticked deletes anything."""
    text = (QML / "AppPage.qml").read_text()
    body = _function(text, "    function openUninstall()")
    plain, archive = body.split("if (busyText !== \"\")", 1)
    assert "deleteFile.checked = false" in plain and plain.index("deleteFile.checked = false") < plain.index("uninstallDialog.open()")
    assert "backend.archiveInfo(installationId)" in archive
    failed = archive[archive.index("if (!r.ok)"):archive.index("archiveInfo = r.result")]
    assert "showError(r.error)" in failed and "return;" in failed and "uninstallDialog.open()" not in failed
    assert archive.index("deleteFile.checked = false") < archive.index("uninstallDialog.open()")
    assert "backend.uninstall(page.installationId, deleteFile.visible && deleteFile.checked)" in text  # a hidden, left-over tick deletes nothing
    assert text.count("backend.uninstall(") == 1


def test_a_late_answer_about_libraries_is_dropped_when_another_program_has_been_opened_since():
    text = (QML / "InstallPage.qml").read_text()
    body = _function(text, "    function checkLibraries(installationId)")
    before_ask = body[:body.index("page.ask(")]
    assert "const seq = analysisSeq" in before_ask and "const name = plan ? plan.name : \"\"" in before_ask
    done, progress = body[body.index("function (l)"):body.index("}, function (line)")], body[body.index("}, function (line)"):]
    assert "if (seq !== analysisSeq)" in done and done.index("if (seq !== analysisSeq)") < done.index("librariesNote =")
    assert "seq === analysisSeq" in progress
    assert "name: name" in done  # the name shown is the program asked about, not whichever is open now
