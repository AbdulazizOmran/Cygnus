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

    function load() {
        loading = true;
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
        busyText = "Preparing…";
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

    function startRepair() {
        if (busyText !== "")
            return;
        busyText = "Checking…";
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
            text: "Check again"
            enabled: page.busyText === ""
            icon.name: "view-refresh"
            onTriggered: page.load()
        },
        Kirigami.Action {
            text: "Repair…"
            enabled: page.busyText === ""
            icon.name: "tools-wizard"
            visible: page.movable
            onTriggered: page.startRepair()
        },
        Kirigami.Action {
            text: "Move to…"
            enabled: page.busyText === ""
            icon.name: "folder-move"
            visible: page.movable
            onTriggered: {
                backend.refreshStorage();
                moveDialog.open();
            }
        },
        Kirigami.Action {
            text: "Uninstall…"
            enabled: page.busyText === ""
            icon.name: "edit-delete"
            visible: page.installationId !== ""
            onTriggered: uninstallDialog.open()
        }
    ]

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            id: errorMessage
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            showCloseButton: true
            visible: false
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
            text: "Cygnus has no detailed knowledge of this application yet"
            explanation: page.report && page.report.error ? root.flat(page.report.error) : "Basic checks only."
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
                            text: "Not managed by Cygnus yet: add a menu entry and launcher that keep working if the drive is unplugged."
                            wrapMode: Text.Wrap
                            opacity: 0.8
                        }
                        QQC2.Button {
                            text: "Manage with Cygnus"
                            icon.name: "list-add"
                            enabled: page.busyText === ""
                            onClicked: page.runWithProgress(backend.adoptAppImage(modelData.where), "Adopting…")
                        }
                    }
                    Kirigami.InlineMessage {
                        Layout.fillWidth: true
                        visible: modelData.runtime_eol
                        type: Kirigami.MessageType.Warning
                        text: "This copy uses a Flatpak runtime that no longer receives security updates."
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
                                text: "Show again"
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
                                text: modelData.code === "BROWSER_EXT_MISSING" ? "Get extension…" : "Install missing component…"
                                icon.name: "list-add"
                                onClicked: page.startFix(modelData.component)
                            }
                            QQC2.Button {
                                visible: !!modelData.dismissible
                                enabled: page.busyText === ""
                                text: "Not interested"
                                icon.name: "dialog-cancel"
                                QQC2.ToolTip.text: "Stop checking and suggesting this optional feature"
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

    Kirigami.PromptDialog {
        id: fixDialog
        title: page.fixPlan ? root.flat(page.fixPlan.title) : ""
        subtitle: page.fixPlan ? root.flat(page.fixPlan.message + (page.fixPlan.security_note ? "\n\n" + page.fixPlan.security_note : "") + (page.fixPlan.relogin ? "\n\nYou will need to log out and back in afterwards." : "")) : ""
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: page.fixPlan && page.fixPlan.kind === "browser" ? "Open store page" : "Continue"
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
                    page.busyText = "Waiting for authorization…";
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

    Kirigami.PromptDialog {
        id: repairDialog
        title: root.flat(page.problems.length ? "Repair " + page.appName + "?" : "Nothing to repair")
        subtitle: root.flat(page.problems.length ? "Cygnus found:\n" + page.problems.map(p => "• " + p.what.replace("_", " ") + " " + p.state + ": " + p.path).join("\n") + "\n\nIt will rewrite what it created. Your settings and data are not touched." : "Everything Cygnus set up for " + page.appName + " is in place.")
        standardButtons: page.problems.length ? Kirigami.Dialog.Cancel : Kirigami.Dialog.Ok
        customFooterActions: [
            Kirigami.Action {
                text: "Repair"
                icon.name: "tools-wizard"
                visible: page.problems.length > 0
                onTriggered: {
                    repairDialog.close();
                    page.runWithProgress(backend.repairApp(page.installationId), "Repairing…");
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: moveDialog
        title: root.flat("Move " + page.appName)
        subtitle: !page.moveTargets.length ? "There is no other connected storage location that can hold this application. Add one on the Storage page." : page.installationFormat === "flatpak" ? "Flatpak installs it in the other installation, then removes the old copy. Your settings and data in ~/.var/app stay where they are." : "The menu entry and launcher follow it, including autostart. The old copy is deleted only after everything else worked."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Move"
                icon.name: "folder-move"
                enabled: page.moveTargets.length > 0
                onTriggered: {
                    moveDialog.close();
                    page.runWithProgress(backend.moveApp(page.installationId, moveTarget.currentText), "Moving…");
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
        subtitle: page.isPackage ? "pacman removes the package. Cygnus shows exactly what will be removed before asking for your password." : page.installationFormat === "flatpak" ? "Flatpak removes the application, and any runtime Cygnus installed for it that nothing else uses. Your settings and data in ~/.var/app are kept." : "The menu entry, icon and launcher Cygnus created will be removed. Your settings and data are kept."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Remove application"
                icon.name: "edit-delete"
                onTriggered: {
                    uninstallDialog.close();
                    if (page.isPackage) {
                        page.busyText = "Preparing…";
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
                    page.busyText = "Removing…";
                    page.progressValue = -1;
                    page.progressLabel = "";
                    page.ask(backend.uninstall(page.installationId, deleteFile.checked), function (r) {
                        page.busyText = "";
                        if (r.ok && r.result.ok)
                            root.showPage(appsPage);
                        else {
                            page.showError(root.why(r));
                        }
                    }, function (line) {
                        if (line !== "")
                            page.progressLabel = line;
                    });
                }
            }
        ]
        QQC2.CheckBox {
            id: deleteFile
            visible: page.installationFormat === "appimage"
            text: "Also delete the application file"
        }
    }

    Component.onCompleted: load()
}
