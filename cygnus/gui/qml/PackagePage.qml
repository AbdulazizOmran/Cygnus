import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// One package from your repositories (or installed): what it is, what it needs, what needs it, its files. Installs and removes it
// through the same confirmation as every other change to the system; the password is asked at the end.
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
        } : undefined);
    }
    title: root.flat(packageName)

    property string packageName: ""
    property string repo: ""
    property var info: null
    property bool loading: true
    property string busyText: ""
    property string errorText: "" // already made safe with root.plain()
    property string doneText: ""
    property var confirmPlan: null // the plan whose window is open
    property var warnPlan: null // "Cygnus did not install this": the question asked before a removal is even planned
    property bool warnTicked: false
    property var older: [] // older versions of this package that pacman's cache still holds: [{version, size, modified}]
    property string stayText: "" // "update first" and the like: why nothing was planned

    function load() {
        loading = true;
        page.ask(backend.packageDetails(packageName, repo), function (r) {
            loading = false;
            if (!r.ok) {
                info = null;
                errorText = root.plain(r.error);
                return;
            }
            info = r.result;
            page.loadOlder();
        });
    }

    function loadOlder() {
        older = [];
        if (!info || !info.installed || info.protected)
            return;
        const name = info.name;
        page.ask(backend.packageOlderVersions(name), function (r) {
            if (r.ok && info && info.name === name)
                older = r.result.versions;
        });
    }

    function planDowngrade(version) {
        stayText = "";
        errorText = "";
        doneText = "";
        busyText = "Preparing…";
        page.ask(backend.planDowngradePackage(info.name, version), function (r) {
            busyText = "";
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            confirmPlan = r.result;
            confirmDialog.open();
        });
    }

    function sizeText(n) {
        return !n ? "" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MiB" : Math.round(n / 1024) + " KiB";
    }
    function dateText(t) {
        return t ? new Date(t * 1000).toLocaleDateString(Qt.locale(), Locale.ShortFormat) : "";
    }
    function stateText(d) {
        return d.state === "installed" ? "installed" : d.state === "available" ? "will be installed" + (d.by !== d.dep ? " (" + d.by + ")" : "") : "NOT AVAILABLE";
    }
    function limited(list, more) {
        return list.join(", ") + (more > 0 ? " … and " + more + " more" : "");
    }

    function planInstall() {
        stayText = "";
        errorText = "";
        doneText = "";
        busyText = "Preparing…";
        page.ask(backend.planInstallPackages(JSON.stringify([info.name])), function (r) {
            busyText = "";
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            if (r.result.refused) {
                stayText = r.result.text;
                return;
            }
            confirmPlan = r.result;
            confirmDialog.open();
        });
    }

    function planRemove(confirmedNotMine) {
        stayText = "";
        errorText = "";
        doneText = "";
        busyText = "Preparing…";
        page.ask(backend.planRemovePackagesByName(JSON.stringify([info.name]), confirmedNotMine), function (r) {
            busyText = "";
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            if (r.result.refused) { // a package Cygnus did not install: ask on purpose, then plan again
                warnPlan = r.result;
                warnTicked = false;
                warnDialog.open();
                return;
            }
            confirmPlan = r.result;
            confirmDialog.open();
        });
    }

    // The plan is handed in by the window's button (not read back from the page): closing the window clears confirmPlan, and when
    // that happens at once (no closing animation) a plan read afterwards would be gone and nothing would run.
    function runPlan(plan) {
        if (busyText !== "" || !plan) // a second click while the window closes must not use the same plan again
            return;
        confirmPlan = null;
        busyText = "Waiting for authorization…";
        page.ask(backend.commitFix(plan.token), function (r) {
            busyText = "";
            if (!r.ok || !r.result.ok) {
                errorText = root.plain(root.why(r));
            } else {
                doneText = root.plain(plan.downgrade ? "Went back to " + plan.downgrade.to + "." : plan.action === "Remove" ? "Removed." : "Installed.");
            }
            page.load();
        }, function (line) {
            if (line.trim() !== "") // a blank line from pacman must not end "busy": that is what keeps a second change from starting
                page.busyText = line;
        });
    }

    actions: [
        Kirigami.Action {
            text: "Website"
            icon.name: "internet-services"
            enabled: !!(page.info && /^https?:\/\//.test(page.info.url))
            onTriggered: Qt.openUrlExternally(page.info.url)
        }
    ]

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            showCloseButton: false
            visible: page.errorText !== ""
            text: page.errorText
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Warning
            visible: page.stayText !== ""
            text: root.plain(page.stayText)
            actions: [
                Kirigami.Action {
                    text: "Open Updates"
                    icon.name: "update-none"
                    onTriggered: root.openPage("updates")
                }
            ]
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Positive
            visible: page.doneText !== ""
            text: page.doneText
        }
        ProgressStrip {
            Layout.fillWidth: true
            running: page.loading || page.busyText !== ""
            text: page.busyText
        }

        ColumnLayout {
            Layout.fillWidth: true
            visible: !!page.info
            spacing: Kirigami.Units.largeSpacing

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 2
                wrapMode: Text.Wrap
                text: page.info ? page.info.name + "  " + page.info.version : ""
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.info ? page.info.desc : ""
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                text: !page.info ? "" : page.info.installed ? "Installed: " + page.info.installed_version + (page.info.reason === "explicit" ? " (chosen by you)" : " (installed for another package)") + (page.info.install_date ? ", " + page.dateText(page.info.install_date) : "") + (page.info.repo ? "" : ". No repository offers it (it came from a file or the AUR).") : "Not installed. Offered by the repository " + page.info.repo + "."
            }
            RowLayout {
                spacing: Kirigami.Units.largeSpacing
                QQC2.Button {
                    visible: !!(page.info && !page.info.installed)
                    enabled: !!(page.info && !page.info.blocked.install) && page.busyText === ""
                    text: "Install…"
                    icon.name: "download"
                    onClicked: page.planInstall()
                }
                QQC2.Button {
                    visible: !!(page.info && page.info.installed)
                    enabled: !!(page.info && !page.info.blocked.remove) && page.busyText === ""
                    text: "Remove…"
                    icon.name: "edit-delete"
                    onClicked: page.planRemove(false)
                }
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: text !== ""
                text: !page.info ? "" : page.info.installed ? (page.info.blocked.remove || "") : (page.info.blocked.install || "")
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.info && page.info.installed && page.info.has_install_script)
                text: "This package runs an install script as administrator when it is installed, updated or removed (pacman does that for every package that has one)."
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                text: "About"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: !page.info ? "" : [page.info.url ? "Website: " + page.info.url : "", page.info.licenses.length ? "Licence: " + page.info.licenses.join(", ") : "", page.info.groups.length ? "Groups: " + page.info.groups.join(", ") : "", page.info.packager ? "Packaged by: " + page.info.packager : "", page.info.package_size ? "Download: " + page.sizeText(page.info.package_size) : "", page.info.installed_size ? "Size on disk: " + page.sizeText(page.info.installed_size) : "", page.info.build_date ? "Built: " + page.dateText(page.info.build_date) : ""].filter(x => x !== "").join("\n")
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                text: "Needs"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: !page.info ? "" : page.info.depends.length ? page.info.depends.map(d => d.dep + "  —  " + page.stateText(d)).join("\n") + (page.info.depends_more ? "\n… and " + page.info.depends_more + " more" : "") : "Nothing else."
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.info && page.info.depends_missing)
                color: Kirigami.Theme.negativeTextColor
                text: "Some of what it needs is not available from your repositories, so it cannot be installed."
            }
            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                visible: !!(page.info && page.info.optdepends.length)
                text: "Optional extras"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.info && page.info.optdepends.length)
                text: !page.info ? "" : page.info.optdepends.map(d => d.dep + (d.installed ? " (installed)" : "") + (d.why ? "  —  " + d.why : "")).join("\n")
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                visible: !!(page.info && page.info.installed)
                text: "Needed by"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.info && page.info.installed)
                text: !page.info || !page.info.installed ? "" : page.info.required_by.length ? page.limited(page.info.required_by, page.info.required_by_more) : "No installed package needs it."
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: !!(page.info && page.info.installed && page.info.optional_for.length)
                text: !page.info || !page.info.installed ? "" : "Optional for: " + page.limited(page.info.optional_for, page.info.optional_for_more)
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                visible: page.older.length > 0
                text: "Go back to an older version"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: page.older.length > 0
                text: "Older versions that your package cache still holds. Going back replaces the installed version with the chosen one; the next system update installs the newer one again."
            }
            Repeater {
                model: page.older
                delegate: RowLayout {
                    Layout.fillWidth: true
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        text: modelData.version + "  ·  " + page.dateText(modelData.modified) + "  ·  " + page.sizeText(modelData.size)
                    }
                    QQC2.Button {
                        text: "Install this version…"
                        icon.name: "go-previous"
                        enabled: page.busyText === ""
                        onClicked: page.planDowngrade(modelData.version)
                    }
                }
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                visible: !!(page.info && (page.info.provides.length || page.info.conflicts.length || page.info.replaces.length))
                text: "Relations"
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.info && (page.info.provides.length || page.info.conflicts.length || page.info.replaces.length))
                text: !page.info ? "" : [page.info.provides.length ? "Provides: " + page.info.provides.join(", ") : "", page.info.conflicts.length ? "Cannot be installed together with: " + page.info.conflicts.join(", ") : "", page.info.replaces.length ? "Replaces: " + page.info.replaces.join(", ") : ""].filter(x => x !== "").join("\n")
            }

            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                level: 3
                visible: !!(page.info && page.info.installed)
                text: page.info && page.info.installed ? "Files (" + page.info.files_total + ")" : ""
            }
            QQC2.TextArea {
                textFormat: TextEdit.PlainText
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 14
                readOnly: true
                wrapMode: TextEdit.NoWrap
                font.family: "monospace"
                visible: !!(page.info && page.info.installed)
                text: !page.info || !page.info.installed ? "" : page.info.files.join("\n") + (page.info.files_total > page.info.files.length ? "\n… and " + (page.info.files_total - page.info.files.length) + " more" : "")
            }
        }
    }

    Component.onCompleted: load()

    Kirigami.Dialog {
        id: confirmDialog
        title: page.confirmPlan ? root.flat(page.confirmPlan.dialog_title) : ""
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 34
        onClosed: if (page.busyText === "")
            page.confirmPlan = null
        customFooterActions: [
            Kirigami.Action {
                objectName: "confirmAction"
                text: page.confirmPlan ? page.confirmPlan.action : "Continue"
                icon.name: page.confirmPlan && page.confirmPlan.action === "Remove" ? "edit-delete" : "download"
                enabled: !!page.confirmPlan
                onTriggered: {
                    const plan = page.confirmPlan;
                    confirmDialog.close();
                    page.runPlan(plan);
                }
            }
        ]
        ColumnLayout {
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.confirmPlan ? root.flat(page.confirmPlan.message + (page.confirmPlan.download_bytes ? " Download: " + Math.round(page.confirmPlan.download_bytes / 1048576) + " MiB." : "")) : ""
            }
            QQC2.TextArea {
                textFormat: TextEdit.PlainText
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 10
                readOnly: true
                font.family: "monospace"
                visible: !!(page.confirmPlan && (page.confirmPlan.upgrades.length || page.confirmPlan.installs.length || page.confirmPlan.removals.length))
                text: page.confirmPlan ? page.confirmPlan.upgrades.map(u => u.name + "  " + u.from + " → " + u.to).concat(page.confirmPlan.installs.map(u => u.name + "  (new) " + u.to + (u.reason ? "  (" + u.reason + ")" : ""))).concat(page.confirmPlan.removals.map(u => u.name + "  removed" + (u.reason ? ": " + u.reason : ""))).join("\n") : ""
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: !!(page.confirmPlan && page.confirmPlan.security_note)
                text: page.confirmPlan ? page.confirmPlan.security_note : ""
            }
        }
    }

    Kirigami.Dialog {
        id: warnDialog
        title: "Remove a package Cygnus did not install?"
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 30
        onClosed: page.warnTicked = false
        customFooterActions: [
            Kirigami.Action {
                objectName: "warnAction"
                text: "Go on…"
                icon.name: "edit-delete"
                enabled: page.warnTicked
                onTriggered: {
                    warnDialog.close();
                    page.planRemove(true);
                }
            }
        ]
        ColumnLayout {
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.warnPlan ? page.warnPlan.text : ""
            }
            QQC2.CheckBox {
                text: "I know what this package is and I want it removed"
                checked: page.warnTicked
                onToggled: page.warnTicked = checked
            }
        }
    }
}
