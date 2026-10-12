import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.ScrollablePage {
    id: page

    // Answers that arrive after this page was closed are dropped instead of being run against destroyed
    // items. The flag lives in a plain object so the callbacks can read it even when the page is gone.
    property var life: ({
            gone: false
        })
    Component.onDestruction: life.gone = true

    function ask(rid, callback, onProgress) {
        const alive = life;
        const what = page.title; // read now: the page may be gone when the answer comes
        root.request(rid, function (r) {
            if (!alive.gone)
                callback(r);
            else
                root.lateAnswer(what, r);
        }, onProgress ? function (line) {
            if (!alive.gone)
                onProgress(line);
        } : undefined, onProgress ? function (value, text) {
            if (!alive.gone) {
                page.progressValue = value;
                page.progressLabel = text;
            }
        } : undefined);
    }
    property real progressValue: -1 // how far the running request really is (0..1); -1 = not known, the bar only shows activity
    property string progressLabel: ""
    title: appName

    property string appName
    property string installationId: ""
    property string installationFormat: ""
    property string installationLocation: "" // the label of the place it is in now: moving it there would do nothing
    property var report: null
    property bool loading: true
    property string busyText: ""
    property var fixPlan: null
    property var browserInfo: null // set when the program is a web browser: whether it can be (or is) the default browser
    property var permissions: null // for a Flatpak: what it may do (see cygnus/core/flatpak_permissions.py)

    function load() {
        loading = true;
        loadBrowser(); // what a Repair or a Move changed (the menu entry) may change whether it can be the default browser
        page.ask(backend.checkApp(appName), function (r) {
            loading = false;
            report = r.ok ? r.result : {
                known: false,
                error: r.error
            };
        });
    }

    function startFix(component) {
        if (busyText !== "") // something else on this page is still going on
            return;
        busyText = qsTr("Preparing…");
        progressValue = -1;
        progressLabel = "";
        page.ask(backend.planFix(report.name, component), function (r) {
            busyText = "";
            if (!r.ok) {
                page.showError(r.error);
                return;
            }
            fixPlan = r.result;
            fixDialog.open();
        });
    }

    property var archiveInfo: null // for a program unpacked from an archive: how big its folder is and what changed in it since
    property var libraryResult: null // what a program from an archive still needs from your repositories
    property var scriptsInfo: null // the scripts the program's archive held (as recorded), with their text, for the "Install scripts" dialog
    property var removalScripts: [] // the removal scripts (prerm, postrm, uninstall) that are still as recorded, offered when uninstalling
    property var removalChoice: ({}) // name -> true for the removal scripts that were ticked

    function chosenRemovalScripts() {
        return removalScripts.filter(x => removalChoice[x.name]).map(x => ({
                    name: x.name,
                    sha256: x.sha256,
                    as: "user"
                }));
    }

    function setRemovalChoice(name, on) {
        const c = Object.assign({}, removalChoice);
        if (on)
            c[name] = true;
        else
            delete c[name];
        removalChoice = c;
    }

    function openScripts() {
        if (busyText !== "")
            return;
        busyText = qsTr("Reading the program's scripts…");
        page.ask(backend.archiveScripts(installationId), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            scriptsInfo = r.result;
            scriptsDialog.open();
        });
    }

    // Runs one script now: as the person, or as administrator through the helper (the usual confirmation, then the password).
    function runScript(name, sha256, admin) {
        scriptsDialog.close();
        if (busyText !== "")
            return;
        if (admin) {
            busyText = qsTr("Preparing…");
            page.ask(backend.planScriptAdmin(installationId, name, sha256), function (r) {
                busyText = "";
                if (!r.ok) {
                    showError(r.error);
                    return;
                }
                fixPlan = r.result;
                fixDialog.open();
            });
            return;
        }
        busyText = "Running " + root.flat(name) + "…";
        progressValue = -1;
        progressLabel = "";
        page.ask(backend.runArchiveScript(installationId, name, sha256), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            const x = r.result.result;
            if (x.ok)
                root.notify(root.flat(name) + " ran without an error.");
            else
                showAttention(name + ": " + (x.error || "it did not finish") + (x.output ? "\n" + x.output.slice(-600) : ""));
        }, function (line) {
            if (line !== "")
                page.progressLabel = line;
        });
    }

    // Asks what removing a program's folder would remove before the question is put.
    function openUninstall() {
        if (installationFormat !== "archive") {
            deleteFile.checked = false;
            removalChoice = ({}); // a choice made for another program never reaches this one
            removalScripts = [];
            uninstallDialog.open();
            return;
        }
        if (busyText !== "")
            return;
        busyText = qsTr("Looking at the program's folder…");
        page.ask(backend.archiveInfo(installationId), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error); // removing without being able to say what is in the folder is not offered
                return;
            }
            archiveInfo = r.result;
            deleteFile.checked = false; // never carried over from another program
            removalChoice = ({}); // nor the choice of removal scripts
            removalScripts = [];
            page.ask(backend.archiveScripts(installationId), function (s) {
                if (s.ok)
                    removalScripts = s.result.scripts.filter(x => x.phase === "remove" && x.state === "ok");
                uninstallDialog.open(); // no removal scripts to offer is not a reason to hold the removal back
            });
        });
    }

    function checkLibraries() {
        if (busyText !== "")
            return;
        busyText = qsTr("Checking the libraries the program needs…");
        progressValue = -1;
        progressLabel = "";
        page.ask(backend.archiveLibraries(installationId), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            libraryResult = r.result;
            if (r.result.packages.length || r.result.unresolved.length || r.result.glibc)
                libraryDialog.open();
            else
                root.notify("Every library " + page.appName + " needs is installed.");
        }, function (line) {
            if (line !== "")
                page.busyText = line;
        });
    }

    function installLibraries() {
        if (busyText !== "" || !libraryResult || !libraryResult.packages.length)
            return;
        busyText = qsTr("Preparing…");
        page.ask(backend.planRepoDependencies(JSON.stringify(libraryResult.packages)), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            fixPlan = r.result;
            fixDialog.open();
        });
    }

    property var needsResult: null // what the assistant suggested for this program, already checked by Cygnus
    property string needsError: "" // shown inside the suggestions window: an error on the page behind it could not be seen
    property string needsAddress: "" // a documentation page the person added

    // Asks the assistant only when the person has turned it on in Settings, and says so when not.
    function askNeeds(extraUrl) {
        if (busyText !== "")
            return;
        busyText = qsTr("Checking the assistant settings…");
        needsError = "";
        page.ask(backend.aiStatus(), function (s) {
            if (!s.ok) {
                busyText = "";
                showError(s.error);
                return;
            }
            if (!s.result.enabled) {
                busyText = "";
                showAttention(qsTr("The AI assistant is off. You can turn it on in Settings; nothing is sent until you do."));
                return;
            }
            busyText = qsTr("Reading the documentation…");
            page.ask(backend.aiDiscover(installationId, extraUrl), function (r) {
                busyText = "";
                if (!r.ok) {
                    showError(r.error);
                    return;
                }
                needsResult = r.result;
                needsDialog.open();
            }, function (line) {
                if (line !== "")
                    page.busyText = line;
            });
        });
    }

    // One suggestion becomes the same kind of plan and confirmation as any other fix.
    function actOnSuggestion(item) {
        if (busyText !== "")
            return;
        if (item.kind === "aur") {
            root.openPage("install", {
                appSpec: "aur:" + item.target
            });
            return;
        }
        busyText = qsTr("Preparing…");
        progressValue = -1;
        progressLabel = "";
        needsError = "";
        page.ask(backend.planSuggestion(item.token), function (r) {
            busyText = "";
            if (!r.ok) {
                needsError = String(r.error); // the window is still open and covers the page: say it there
                return;
            }
            needsDialog.close();
            fixPlan = r.result;
            fixDialog.open();
        });
    }

    property var problems: []
    readonly property bool isPackage: installationFormat === "pacman" || installationFormat === "aur"
    property var storage: JSON.parse(backend ? backend.storageJson : "{\"locations\": [], \"candidates\": []}")
    readonly property var moveTargets: storage.locations.filter(l => l.online && (installationFormat === "flatpak" ? l.flatpak : l.appimages) && l.label.toLowerCase() !== installationLocation.toLowerCase()).map(l => l.label)
    readonly property bool managedAppImage: installationId !== "" && installationFormat === "appimage"
    readonly property bool movable: installationId !== "" && (installationFormat === "appimage" || installationFormat === "flatpak")

    function showError(text) {
        errorMessage.type = Kirigami.MessageType.Error;
        errorMessage.text = root.plain(text);
        errorMessage.visible = true;
    }

    function showAttention(text) { // it finished, but something needs a look: not an error
        errorMessage.type = Kirigami.MessageType.Warning;
        errorMessage.text = root.plain(text);
        errorMessage.visible = true;
    }

    function runWithProgress(rid, label) {
        busyText = label;
        progressValue = -1;
        progressLabel = "";
        page.ask(rid, function (r) {
            busyText = "";
            if (!r.ok || !r.result.ok)
                showError(root.why(r));
            else if (r.result.attention)
                showAttention(r.result.attention);
            page.load();
        }, function (line) {
            if (line === "")
                return;
            page.busyText = line;
            page.progressLabel = line;
        });
    }

    function loadBrowser() {
        if (installationId === "")
            return;
        page.ask(backend.defaultBrowserInfo(installationId), function (r) {
            browserInfo = r.ok && r.result.browser ? r.result : null;
        });
    }

    function openPermissions() {
        if (busyText !== "")
            return;
        busyText = qsTr("Reading the permissions…");
        page.ask(backend.flatpakPermissions(installationId), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            permissions = r.result;
            permissionsDialog.open();
        });
    }

    // The permissions as rows for the dialog: a heading for each group, then its items (the worst first).
    function permissionRows() {
        if (!permissions)
            return [];
        const rows = [];
        permissions.groups.forEach(g => {
            rows.push({
                heading: g.title
            });
            permissions.items.filter(i => i.group === g.id).forEach(i => rows.push(i));
        });
        return rows;
    }

    function permissionTag(item) {
        return (item.level === "escape" ? qsTr("  ·  can leave the sandbox") : item.level === "broad" ? qsTr("  ·  broad access") : "") + (item.source === "you" ? qsTr("  ·  changed by you") : item.source === "system" ? qsTr("  ·  changed for the whole system") : "");
    }

    function showDone(text) {
        errorMessage.type = Kirigami.MessageType.Positive;
        errorMessage.text = root.plain(text);
        errorMessage.visible = true;
    }

    // Makes the program the one links open with, or puts back what was set before it. Cygnus reads the result back from
    // every place the desktop looks, so an answer of "ok" means it really took.
    function setBrowser(on) {
        if (busyText !== "" || !browserInfo)
            return;
        const name = browserInfo.name;
        busyText = on ? "Making " + name + " the default browser…" : qsTr("Putting your earlier choice back…");
        page.ask(backend.setDefaultBrowser(browserInfo.desktop_id, on), function (r) {
            busyText = "";
            if (!r.ok)
                showError(r.error);
            else if (on)
                showDone(name + " is now your default browser: links you click in other programs open there.");
            else
                showDone(qsTr("What was set before is back."));
            page.loadBrowser();
        });
    }

    function startRepair() {
        if (busyText !== "")
            return;
        busyText = qsTr("Checking…");
        page.ask(backend.diagnoseApp(installationId), function (r) {
            busyText = "";
            if (!r.ok) {
                showError(r.error);
                return;
            }
            problems = r.result;
            repairDialog.open();
        });
    }

    actions: [
        Kirigami.Action {
            text: qsTr("Check again")
            enabled: page.busyText === ""
            icon.name: "view-refresh"
            onTriggered: page.load()
        },
        Kirigami.Action {
            text: qsTr("Repair…")
            enabled: page.busyText === ""
            icon.name: "tools-wizard"
            visible: page.movable
            onTriggered: page.startRepair()
        },
        Kirigami.Action {
            text: qsTr("Move to…")
            enabled: page.busyText === ""
            icon.name: "folder-move"
            visible: page.movable
            onTriggered: {
                backend.refreshStorage();
                moveDialog.open();
            }
        },
        Kirigami.Action {
            objectName: "browserAction"
            text: !!page.browserInfo && page.browserInfo.is_default && page.browserInfo.remembered ? qsTr("Put back the earlier browser…") : qsTr("Make default browser…")
            enabled: page.busyText === ""
            icon.name: "internet-web-browser"
            visible: !!page.browserInfo && !(page.browserInfo.is_default && !page.browserInfo.remembered)
            onTriggered: browserDialog.open()
        },
        Kirigami.Action {
            objectName: "permissionsAction"
            text: qsTr("Permissions…")
            enabled: page.busyText === ""
            icon.name: "security-medium"
            visible: page.installationFormat === "flatpak"
            onTriggered: page.openPermissions()
        },
        Kirigami.Action {
            text: qsTr("Check libraries…")
            enabled: page.busyText === ""
            icon.name: "system-search"
            visible: page.installationFormat === "archive"
            onTriggered: page.checkLibraries()
        },
        Kirigami.Action {
            text: qsTr("Install scripts…")
            enabled: page.busyText === ""
            icon.name: "utilities-terminal"
            visible: page.installationFormat === "archive"
            onTriggered: page.openScripts()
        },
        Kirigami.Action {
            text: qsTr("What else does this need?")
            enabled: page.busyText === ""
            icon.name: "help-hint"
            visible: page.installationId !== ""
            onTriggered: page.askNeeds("")
        },
        Kirigami.Action {
            text: qsTr("Uninstall…")
            enabled: page.busyText === ""
            icon.name: "edit-delete"
            visible: page.installationId !== ""
            onTriggered: page.openUninstall()
        }
    ]

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            id: errorMessage
            objectName: "pageMessage"
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            showCloseButton: true
            visible: false
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Information
            visible: !!page.browserInfo && (page.browserInfo.is_default || page.browserInfo.by_accident)
            text: !page.browserInfo ? "" : root.plain(page.browserInfo.is_default ? qsTr("This is your default web browser: links you click in other programs open here.") : page.browserInfo.name + " opens links only because nothing else is chosen. Choose it on purpose to make that stick.")
        }
        ProgressStrip {
            Layout.fillWidth: true
            running: page.loading || page.busyText !== ""
            value: page.busyText !== "" ? page.progressValue : -1
            text: page.busyText !== "" ? (page.progressLabel !== "" ? page.progressLabel : page.busyText) : ""
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: !page.loading && page.report && !page.report.known
            text: qsTr("Cygnus has no detailed knowledge of this application yet")
            explanation: page.report && page.report.error ? root.flat(page.report.error) : qsTr("Basic checks only.")
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: !page.loading && page.report && page.report.known && page.report.installs.length === 0
            text: root.flat(page.appName) + " is not installed"
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Warning
            visible: !!(page.report && page.report.duplicate)
            text: page.report && page.report.duplicate ? root.plain(page.report.duplicate.title + ". " + page.report.duplicate.explanation) : ""
        }

        Repeater {
            model: page.report && page.report.installs ? page.report.installs : []
            delegate: Kirigami.Card {
                Layout.fillWidth: true
                banner.title: root.flat(modelData.symbol + "  " + page.report.name + " (" + modelData.format + ")")
                banner.titleIcon: modelData.overall === "ok" ? "emblem-ok-symbolic" : "data-warning"
                contentItem: ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label {
                        textFormat: Text.PlainText
                        text: modelData.where + (modelData.running ? "  (running)" : "") + (modelData.version ? "  ·  version " + modelData.version : "")
                        opacity: 0.7
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: !!backend && modelData.format === "appimage" && !backend.isManaged(modelData.where)
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            text: qsTr("Not managed by Cygnus yet: add a menu entry and launcher that keep working if the drive is unplugged.")
                            wrapMode: Text.Wrap
                            opacity: 0.8
                        }
                        QQC2.Button {
                            text: qsTr("Manage with Cygnus")
                            icon.name: "list-add"
                            enabled: page.busyText === ""
                            onClicked: page.runWithProgress(backend.adoptAppImage(modelData.where), qsTr("Adopting…"))
                        }
                    }
                    Kirigami.InlineMessage {
                        Layout.fillWidth: true
                        visible: modelData.runtime_eol
                        type: Kirigami.MessageType.Warning
                        text: qsTr("This copy uses a Flatpak runtime that no longer receives security updates.")
                    }
                    GridLayout {
                        columns: 3
                        columnSpacing: Kirigami.Units.largeSpacing
                        Layout.fillWidth: true
                        Repeater {
                            model: modelData.features
                            delegate: QQC2.Label {
                                textFormat: Text.PlainText
                                text: modelData.name
                                font.bold: true
                                Layout.row: index
                                Layout.column: 0
                            }
                        }
                        Repeater {
                            model: modelData.features
                            delegate: QQC2.Label {
                                textFormat: Text.PlainText
                                text: modelData.symbol
                                Layout.row: index
                                Layout.column: 1
                            }
                        }
                        Repeater {
                            model: modelData.features
                            delegate: QQC2.Label {
                                textFormat: Text.PlainText
                                text: modelData.status === "ok" ? "" : modelData.explanation
                                wrapMode: Text.Wrap
                                opacity: 0.8
                                Layout.fillWidth: true
                                Layout.row: index
                                Layout.column: 2
                            }
                        }
                    }
                    Repeater {
                        model: modelData.features.filter(f => f.status === "dismissed")
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                opacity: 0.7
                                text: "You switched off: " + modelData.name
                            }
                            QQC2.Button {
                                text: qsTr("Show again")
                                flat: true
                                enabled: page.busyText === ""
                                onClicked: {
                                    const ids = modelData.dismissed_components;
                                    let pending = ids.length;
                                    ids.forEach(id => page.ask(backend.dismissComponent(page.report.name, id, false), function () {
                                            if (--pending === 0)
                                                page.load();
                                        }));
                                }
                            }
                        }
                    }
                    Repeater {
                        model: modelData.issues
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            QQC2.Label {
                                textFormat: Text.PlainText
                                text: modelData.title + (modelData.resolutions.length ? " — " + modelData.resolutions[0].explanation : "")
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            QQC2.Button {
                                visible: modelData.resolutions.length > 0 && !!modelData.component
                                enabled: page.busyText === ""
                                text: modelData.code === "BROWSER_EXT_MISSING" ? qsTr("Get extension…") : qsTr("Install missing component…")
                                icon.name: "list-add"
                                onClicked: page.startFix(modelData.component)
                            }
                            QQC2.Button {
                                visible: !!modelData.dismissible
                                enabled: page.busyText === ""
                                text: qsTr("Not interested")
                                icon.name: "dialog-cancel"
                                QQC2.ToolTip.text: qsTr("Stop checking and suggesting this optional feature")
                                QQC2.ToolTip.visible: hovered
                                onClicked: page.ask(backend.dismissComponent(page.report.name, modelData.component, true), function (r) {
                                    if (!r.ok)
                                        page.showError(r.error);
                                    page.load();
                                })
                            }
                        }
                    }
                }
            }
        }
    }

    Kirigami.Dialog {
        id: needsDialog
        title: root.flat(page.needsResult ? "What " + page.needsResult.app + " may still need" : "")
        preferredWidth: Kirigami.Units.gridUnit * 36
        standardButtons: Kirigami.Dialog.Close
        onClosed: page.needsError = ""
        ColumnLayout {
            spacing: Kirigami.Units.largeSpacing
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                visible: page.needsError !== ""
                type: Kirigami.MessageType.Error
                text: root.plain(page.needsError)
            }
            QQC2.Label {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                text: !page.needsResult ? "" : page.needsResult.suggestions.length === 0 ? (page.needsResult.needs_address ? qsTr("Cygnus does not know a documentation page for this program. Paste one below and ask again.") : "Nothing was suggested. " + (page.needsResult.pages.length ? "Pages read: " + page.needsResult.pages.join(", ") : qsTr("No documentation page could be read."))) : "These come from an AI assistant reading the program's own documentation (" + page.needsResult.provider + "). For each one Cygnus checked that the quoted sentence really is on the page, and that a suggested package or group exists, but the AI can still be wrong: only go on with what makes sense for you. Nothing is installed without your approval."
            }
            Repeater {
                model: page.needsResult ? page.needsResult.suggestions : []
                delegate: Kirigami.AbstractCard {
                    required property var modelData
                    Layout.fillWidth: true
                    contentItem: ColumnLayout {
                        spacing: Kirigami.Units.smallSpacing
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.bold: true
                            textFormat: Text.PlainText
                            text: modelData.name + (modelData.relation === "required" ? qsTr(" (it says this is required)") : " (optional)")
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            text: "AI suggestion, unverified. " + modelData.why
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.italic: true
                            textFormat: Text.PlainText
                            text: "The page says: \u201c" + modelData.quote + "\u201d"
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: Kirigami.Theme.disabledTextColor
                            text: "Source: " + modelData.citation_url + (modelData.source ? "   Found in: " + modelData.source : "")
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            visible: modelData.notes.length > 0
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            text: modelData.notes.join(" ")
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            visible: modelData.satisfied !== ""
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            text: "Nothing to do: " + modelData.satisfied
                        }
                        RowLayout {
                            visible: modelData.satisfied === "" && modelData.kind !== "info"
                            QQC2.Button {
                                text: modelData.kind === "aur" ? qsTr("Review it on the Install page") : modelData.kind === "extension" ? qsTr("Open the store page…") : modelData.kind === "group" ? qsTr("Add me to the group…") : modelData.kind === "service" ? qsTr("Start it…") : qsTr("Install…")
                                enabled: page.busyText === ""
                                onClicked: page.actOnSuggestion(modelData)
                            }
                            QQC2.Button {
                                text: qsTr("Open the page it came from")
                                onClicked: Qt.openUrlExternally(modelData.citation_url)
                            }
                        }
                    }
                }
            }
            QQC2.Label {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                visible: !!page.needsResult && page.needsResult.left_out.length > 0
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: Kirigami.Theme.disabledTextColor
                text: !page.needsResult ? "" : "Left out: " + page.needsResult.left_out.join(" ")
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                QQC2.TextField {
                    id: needsAddressField
                    Layout.fillWidth: true
                    placeholderText: qsTr("Another documentation page (https), if you know one")
                }
                QQC2.Button {
                    text: qsTr("Ask again")
                    enabled: page.busyText === ""
                    onClicked: {
                        needsDialog.close();
                        page.askNeeds(needsAddressField.text);
                    }
                }
            }
        }
    }

    Kirigami.PromptDialog {
        id: scriptsDialog
        title: root.flat("Install scripts of " + page.appName)
        subtitle: qsTr("These came with the program's archive. Nothing here runs unless you press one of the Run buttons; each one runs exactly the text shown, and only while it is still as it was when the program was installed.")
        standardButtons: Kirigami.Dialog.Close
        ColumnLayout {
            spacing: Kirigami.Units.largeSpacing
            QQC2.Label {
                textFormat: Text.PlainText
                visible: !!page.scriptsInfo && page.scriptsInfo.scripts.length === 0
                text: qsTr("This program's archive had no install scripts.")
            }
            Repeater {
                model: page.scriptsInfo ? page.scriptsInfo.scripts : []
                delegate: ColumnLayout {
                    id: scriptRow
                    required property var modelData
                    Layout.fillWidth: true
                    Kirigami.Heading {
                        textFormat: Text.PlainText
                        level: 5
                        text: scriptRow.modelData.name + (scriptRow.modelData.phase === "remove" ? qsTr(" (for removing the program)") : "")
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        visible: scriptRow.modelData.state !== "ok"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        text: scriptRow.modelData.state === "changed" ? qsTr("It has been changed since the program was installed, so it will not be run.") : qsTr("It is not there any more.")
                    }
                    QQC2.ScrollView {
                        visible: scriptRow.modelData.state === "ok"
                        Layout.fillWidth: true
                        Layout.preferredHeight: Kirigami.Units.gridUnit * 10
                        QQC2.TextArea {
                            textFormat: TextEdit.PlainText
                            readOnly: true
                            font.family: "monospace"
                            wrapMode: TextEdit.NoWrap
                            text: scriptRow.modelData.text || ""
                        }
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        visible: scriptRow.modelData.state === "ok" && scriptRow.modelData.phase !== "install"
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        opacity: 0.7
                        text: qsTr("It is offered when you remove the program.")
                    }
                    RowLayout {
                        visible: scriptRow.modelData.state === "ok" && scriptRow.modelData.phase === "install"
                        QQC2.Button {
                            text: qsTr("Run as me")
                            icon.name: "media-playback-start"
                            enabled: page.busyText === ""
                            onClicked: page.runScript(scriptRow.modelData.name, scriptRow.modelData.sha256, false)
                        }
                        QQC2.Button {
                            text: qsTr("Run as administrator…")
                            icon.name: "dialog-password"
                            enabled: page.busyText === ""
                            onClicked: page.runScript(scriptRow.modelData.name, scriptRow.modelData.sha256, true)
                        }
                    }
                }
            }
        }
    }

    Kirigami.PromptDialog {
        id: libraryDialog
        title: root.flat("What " + page.appName + " is missing")
        subtitle: !page.libraryResult ? "" : root.flat((page.libraryResult.glibc ? "It was built for a newer C library (" + page.libraryResult.glibc.needs + ") than this computer has (" + page.libraryResult.glibc.have + "), so it may not start. " : "") + (page.libraryResult.packages.length ? "Libraries it needs are not installed here. Your repositories have them in: " + page.libraryResult.packages.join(", ") + ". " : "") + (page.libraryResult.unresolved.length ? "No repository provides: " + page.libraryResult.unresolved.slice(0, 6).join(", ") + (page.libraryResult.unresolved.length > 6 ? qsTr(" and more") : "") + ". It may not start. " : "") + (page.libraryResult.optional.length ? "Some parts also want: " + page.libraryResult.optional.map(o => o.package).join(", ") + " (optional)." : ""))
        standardButtons: Kirigami.Dialog.Close
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Install the missing libraries…")
                icon.name: "download"
                visible: !!page.libraryResult && page.libraryResult.packages.length > 0
                onTriggered: {
                    libraryDialog.close();
                    page.installLibraries();
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: fixDialog
        title: page.fixPlan ? root.flat(page.fixPlan.title) : ""
        subtitle: page.fixPlan ? root.flat(page.fixPlan.message + (page.fixPlan.security_note ? "\n\n" + page.fixPlan.security_note : "") + (page.fixPlan.relogin ? qsTr("\n\nYou will need to log out and back in afterwards.") : "")) : ""
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: page.fixPlan && page.fixPlan.kind === "browser" ? qsTr("Open store page") : "Continue"
                icon.name: "dialog-ok"
                enabled: page.fixPlan && page.fixPlan.kind !== "info"
                onTriggered: {
                    if (page.busyText !== "") // a second click while the dialog closes
                        return;
                    fixDialog.close();
                    if (page.fixPlan.kind === "browser") {
                        Qt.openUrlExternally(page.fixPlan.urls[0]);
                        return;
                    }
                    page.busyText = qsTr("Waiting for authorization…");
                    page.progressValue = -1;
                    page.progressLabel = "";
                    const plan = page.fixPlan; // what happens afterwards belongs to this very plan
                    page.ask(backend.commitFix(plan.token), function (r) {
                        page.busyText = "";
                        if (!r.ok || !r.result.ok) {
                            page.showError(root.why(r));
                        } else if (plan.forgetAfter) { // the backend already forgot this installation's record
                            root.showPage(appsPage);
                            return;
                        }
                        page.load();
                    }, function (line) {
                        if (line !== "")
                            page.progressLabel = line; // pacman's own counter, once the administrator part is running
                    });
                }
            }
        ]
    }

    Kirigami.Dialog {
        id: permissionsDialog
        objectName: "permissionsDialog"
        title: root.flat(page.permissions ? page.permissions.name + ": what it may do" : "Permissions")
        standardButtons: Kirigami.Dialog.Close
        preferredWidth: Kirigami.Units.gridUnit * 36
        ColumnLayout {
            spacing: Kirigami.Units.smallSpacing
            QQC2.Label {
                objectName: "permissionsSummary"
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                wrapMode: Text.Wrap
                font.bold: true
                text: !page.permissions ? "" : page.permissions.summary.escape + " that can leave the sandbox · " + page.permissions.summary.broad + " broad · " + page.permissions.summary.total + " in all"
            }
            Repeater {
                model: page.permissionRows()
                QQC2.Label {
                    required property var modelData
                    objectName: modelData.heading !== undefined ? "permissionHeading" : "permissionItem"
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing * (modelData.heading !== undefined ? 1 : 2)
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: modelData.heading !== undefined ? Kirigami.Units.largeSpacing : 0
                    wrapMode: Text.Wrap
                    font.bold: modelData.heading !== undefined || modelData.level === "escape"
                    text: modelData.heading !== undefined ? modelData.heading : (modelData.level === "escape" ? "⚠ " : "• ") + modelData.text + page.permissionTag(modelData)
                }
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                wrapMode: Text.Wrap
                opacity: 0.7
                visible: !!page.permissions
                text: !page.permissions ? "" : (page.permissions.removed.length ? page.permissions.removed.join("\n") + "\n\n" : "") + (page.permissions.notes.length ? page.permissions.notes.join("\n") + "\n\n" : "") + "This is what the application asks for, with the changes made on this computer (by Flatseal or flatpak override). Cygnus only shows it: change it with Flatseal or the flatpak override command."
            }
        }
    }

    Kirigami.PromptDialog {
        id: browserDialog
        objectName: "browserDialog"
        readonly property bool putBack: !!page.browserInfo && page.browserInfo.is_default && page.browserInfo.remembered
        title: root.flat(!page.browserInfo ? "" : putBack ? qsTr("Put back what was set before?") : "Make " + page.browserInfo.name + " your default browser?")
        subtitle: root.flat(!page.browserInfo ? "" : (putBack ? "Links will open in what they opened in before you chose " + page.browserInfo.name + " (a choice you changed since is left as it is)." : "Links you click in other programs (chat, mail, the terminal) will open in " + page.browserInfo.name + (page.browserInfo.current_name ? " instead of " + page.browserInfo.current_name : "") + ". What is set now is remembered, so you can put it back from this page.") + (page.browserInfo.blockers.length ? "\n\nThis cannot be done yet:\n• " + page.browserInfo.blockers.join("\n• ") : "") + (!putBack && page.browserInfo.notes.length ? "\n\nGood to know:\n• " + page.browserInfo.notes.join("\n• ") : ""))
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                objectName: "browserDialogAction"
                text: browserDialog.putBack ? qsTr("Put back") : qsTr("Make default")
                icon.name: "internet-web-browser"
                enabled: !!page.browserInfo && (browserDialog.putBack || page.browserInfo.can)
                onTriggered: {
                    browserDialog.close();
                    page.setBrowser(!browserDialog.putBack);
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: repairDialog
        title: root.flat(page.problems.length ? "Repair " + page.appName + "?" : qsTr("Nothing to repair"))
        subtitle: root.flat(page.problems.length ? "Cygnus found:\n" + page.problems.map(p => "• " + p.what.replace("_", " ") + " " + p.state + ": " + p.path).join("\n") + "\n\nIt will rewrite what it created. Your settings and data are not touched." : "Everything Cygnus set up for " + page.appName + " is in place.")
        standardButtons: page.problems.length ? Kirigami.Dialog.Cancel : Kirigami.Dialog.Ok
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Repair")
                icon.name: "tools-wizard"
                visible: page.problems.length > 0
                onTriggered: {
                    repairDialog.close();
                    page.runWithProgress(backend.repairApp(page.installationId), qsTr("Repairing…"));
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: moveDialog
        title: root.flat("Move " + page.appName)
        subtitle: !page.moveTargets.length ? qsTr("There is no other connected storage location that can hold this application. Add one on the Storage page.") : page.installationFormat === "flatpak" ? qsTr("Flatpak installs it in the other installation, then removes the old copy. Your settings and data in ~/.var/app stay where they are.") : qsTr("The menu entry and launcher follow it, including autostart. The old copy is deleted only after everything else worked.")
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Move")
                icon.name: "folder-move"
                enabled: page.moveTargets.length > 0
                onTriggered: {
                    moveDialog.close();
                    page.runWithProgress(backend.moveApp(page.installationId, moveTarget.currentText), qsTr("Moving…"));
                }
            }
        ]
        QQC2.ComboBox {
            id: moveTarget
            visible: page.moveTargets.length > 0
            model: page.moveTargets
        }
    }

    Kirigami.PromptDialog {
        id: uninstallDialog
        title: root.flat("Remove " + page.appName + "?")
        subtitle: page.isPackage ? qsTr("pacman removes the package. Cygnus shows exactly what will be removed before asking for your password.") : page.installationFormat === "flatpak" ? qsTr("Flatpak removes the application, and any runtime Cygnus installed for it that nothing else uses. Your settings and data in ~/.var/app are kept.") : page.installationFormat === "archive" ? "The menu entry, icon and launcher Cygnus created will be removed. What the program saved in your home folder (its settings and data) is kept. " + (!!page.archiveInfo && page.archiveInfo.present && page.archiveInfo.ours ? qsTr("The program's own folder is kept too unless you tick the box below.") : qsTr("The program's own folder is left alone: it is gone, or it is not one Cygnus made.")) : qsTr("The menu entry, icon and launcher Cygnus created will be removed. Your settings and data are kept.")
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Remove application")
                icon.name: "edit-delete"
                onTriggered: {
                    uninstallDialog.close();
                    if (page.isPackage) {
                        page.busyText = qsTr("Preparing…");
                        page.progressValue = -1;
                        page.progressLabel = "";
                        page.ask(backend.planUninstallPackages(page.installationId), function (r) {
                            page.busyText = "";
                            if (!r.ok) {
                                page.showError(r.error);
                                return;
                            }
                            page.fixPlan = Object.assign({}, r.result, {
                                forgetAfter: true
                            });
                            fixDialog.open();
                        });
                        return;
                    }
                    page.busyText = qsTr("Removing…");
                    page.progressValue = -1;
                    page.progressLabel = "";
                    const chosen = page.chosenRemovalScripts();
                    page.ask(chosen.length ? backend.uninstallArchive(page.installationId, deleteFile.visible && deleteFile.checked, JSON.stringify(chosen)) : backend.uninstall(page.installationId, deleteFile.visible && deleteFile.checked), function (r) {
                        page.busyText = "";
                        if (r.ok && r.result.ok) {
                            if (r.result.attention)
                                root.notify(root.flat(r.result.attention)); // the page is about to go: a script that did not finish is said here
                            root.showPage(appsPage);
                        } else {
                            page.showError(root.why(r));
                        }
                    }, function (line) {
                        if (line !== "")
                            page.progressLabel = line;
                    });
                }
            }
        ]
        Repeater {
            model: page.installationFormat === "archive" ? page.removalScripts : []
            delegate: ColumnLayout {
                id: removalRow
                required property var modelData
                Layout.fillWidth: true
                QQC2.ScrollView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.gridUnit * 6
                    QQC2.TextArea {
                        textFormat: TextEdit.PlainText
                        readOnly: true
                        font.family: "monospace"
                        wrapMode: TextEdit.NoWrap
                        text: removalRow.modelData.text || ""
                    }
                }
                QQC2.CheckBox {
                    id: removalBox
                    Layout.fillWidth: true
                    checked: !!page.removalChoice[removalRow.modelData.name]
                    onToggled: page.setRemovalChoice(removalRow.modelData.name, checked)
                    text: "Run " + removalRow.modelData.name + " first (as me, exactly the text above)"
                    contentItem: QQC2.Label {
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        leftPadding: removalBox.indicator ? removalBox.indicator.width + removalBox.spacing : 0
                        text: removalBox.text
                    }
                }
            }
        }
        QQC2.CheckBox {
            id: deleteFile
            visible: page.installationFormat === "appimage" || (page.installationFormat === "archive" && !!page.archiveInfo && page.archiveInfo.present && page.archiveInfo.ours)
            text: page.installationFormat === "archive" && page.archiveInfo ? "Also delete the program's folder (" + page.archiveInfo.files + " files" + (page.archiveInfo.changed || page.archiveInfo.added ? "; " + (page.archiveInfo.changed || 0) + " changed and " + (page.archiveInfo.added || 0) + " added since it was unpacked, so it may hold things the program saved itself" : "") + ")" : qsTr("Also delete the application file")
        }
    }

    Component.onCompleted: load()
}
