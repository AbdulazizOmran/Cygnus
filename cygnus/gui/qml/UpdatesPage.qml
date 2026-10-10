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
    title: "Updates"

    property var updates: []
    property var system: null // last check of your repositories (see cygnus.core.updates)
    property var cygnus: null // the last answer to "is there a newer Cygnus?" (core/self_update.py); it only says so, nothing is downloaded
    property var upgradePlan: null
    property bool newsRead: false // the box under the Arch news: it starts clear for every plan and is never ticked for you
    property bool checking: false
    property string progressText: ""

    function load() {
        page.ask(backend.cygnusUpdate(), function (r) {
            if (r.ok)
                page.cygnus = r.result;
        });
        page.ask(backend.updateOverview(), function (r) {
            if (r.ok)
                page.updates = r.result;
        });
        page.ask(backend.systemUpdateOverview(), function (r) {
            if (r.ok)
                page.system = r.result;
        });
    }

    function planUpgrade() {
        checking = true;
        progressValue = -1;
        progressLabel = "";
        progressText = "Downloading the current package databases…";
        page.ask(backend.planSystemUpgrade(), function (r) {
            checking = false;
            if (!r.ok) {
                errorMessage.text = root.plain(r.error);
                errorMessage.visible = true;
                return;
            }
            upgradePlan = r.result;
            newsRead = false;
            upgradeDialog.open();
        });
    }

    // An Arch news item that says it needs a manual step must be read before the system is updated.
    function newsNeedsAck() {
        return !!(upgradePlan && upgradePlan.news && upgradePlan.news.ok && upgradePlan.news.needs_ack);
    }

    function runUpgrade() {
        if (checking || (newsNeedsAck() && !newsRead)) // a second click while the dialog closes must not use the same plan again
            return;
        checking = true;
        progressValue = -1;
        progressLabel = "";
        progressText = "Waiting for authorization…";
        page.ask(backend.commitFix(upgradePlan.token), function (r) {
            checking = false;
            if (!r.ok || !r.result.ok) {
                errorMessage.text = root.plain(root.why(r));
                errorMessage.visible = true;
            }
            page.checkNow();
        }, function (line) {
            page.progressText = line;
            page.progressLabel = line;
        });
    }

    function checkNow() {
        checking = true;
        progressValue = -1;
        progressLabel = "";
        progressText = "";
        page.ask(backend.checkUpdates(), function (r) {
            checking = false;
            page.ask(backend.cygnusUpdate(), function (c) {
                if (c.ok)
                    page.cygnus = c.result;
            });
            if (r.ok)
                page.updates = r.result;
            else {
                errorMessage.text = root.plain(r.error);
                errorMessage.visible = true;
            }
        }, function (line) {
            page.progressText = line;
            page.progressLabel = line;
        });
    }

    function applyUpdate(u) {
        checking = true;
        progressValue = -1;
        progressLabel = "";
        progressText = "";
        page.ask(backend.applyUpdate(u.installation_id), function (r) {
            checking = false;
            if (!r.ok || !r.result.ok) {
                errorMessage.text = root.plain(root.why(r));
                errorMessage.visible = true;
            }
            page.load();
        }, function (line) {
            page.progressText = line;
            page.progressLabel = line;
        });
    }

    // A program Cygnus converted from a vendor's .deb/.rpm: the newer file is downloaded, then opened on the Install page, where it
    // is analysed and confirmed like any file you chose. Nothing is installed from here.
    function fetchUpdate(u) {
        checking = true;
        progressValue = -1;
        progressLabel = "";
        progressText = "";
        page.ask(backend.fetchConvertedUpdate(u.installation_id), function (r) {
            checking = false;
            if (!r.ok || !r.result.ok) {
                errorMessage.text = root.plain(root.why(r));
                errorMessage.visible = true;
                page.load();
                return;
            }
            root.openPage("install", {
                "fileUrl": r.result.file_url,
                "launchNotice": "Cygnus downloaded " + (r.result.checked ? r.result.name + " " + r.result.version + " from the vendor and checked it against the checksum the vendor publishes" : "the newest " + r.result.name + " from the vendor (the vendor publishes no checksum for it)") + ". Look at what it would do below; nothing is installed until you confirm."
            });
        }, function (line) {
            page.progressText = line;
            page.progressLabel = line;
        });
    }

    function describe(provider) {
        switch (provider) {
        case "vendor-feed":
            return "Source: the vendor's package list";
        case "zsync":
            return "Source: the vendor's download server";
        case "gh-releases-zsync":
        case "gh-releases-direct":
            return "Source: the vendor's GitHub releases";
        case "flatpak-remote":
            return "Source: its Flatpak repository";
        case "pacman":
            return "Updated with your system";
        case "aur":
            return "Rebuilt from the AUR after you review changes";
        default:
            return "Manual updates";
        }
    }

    function statusText(u) {
        switch (u.status) {
        case "up-to-date":
            return "Up to date";
        case "update-available":
            return u.available ? "Update available: " + u.available : "Update available";
        case "offline":
            return "Drive not connected";
        case "manual":
            return "Manual updates only";
        case "system":
            return "Updated with your system";
        default:
            return u.checked_at ? "Could not check" : "Not checked yet";
        }
    }

    function statusIcon(u) {
        switch (u.status) {
        case "up-to-date":
            return "emblem-ok-symbolic";
        case "update-available":
            return "update-none";
        case "offline":
            return "drive-harddisk";
        case "manual":
        case "system":
            return "documentation";
        default:
            return u.checked_at ? "data-warning" : "view-refresh";
        }
    }

    actions: [
        Kirigami.Action {
            text: page.checking ? "Checking…" : "Check now"
            icon.name: "view-refresh"
            enabled: !page.checking
            onTriggered: page.checkNow()
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
        Kirigami.InlineMessage {
            id: cygnusMessage
            Layout.fillWidth: true
            visible: !!(page.cygnus && page.cygnus.ok && page.cygnus.newer)
            type: Kirigami.MessageType.Positive
            text: !page.cygnus ? "" : "Cygnus " + root.plain(page.cygnus.latest) + " is available (you have " + root.plain(page.cygnus.current) + "). Cygnus does not update itself: get the new version the way you got this one (a package file you downloaded: sudo pacman -U FILE; or through your AUR helper if you installed it from the AUR). The button opens its release page."
            actions: [
                Kirigami.Action {
                    text: "Open the release page"
                    icon.name: "internet-services"
                    onTriggered: Qt.openUrlExternally(page.cygnus.url)
                }
            ]
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: !!(page.cygnus && page.cygnus.last_error)
            type: Kirigami.MessageType.Warning
            text: !page.cygnus || !page.cygnus.last_error ? "" : "Cygnus could not look for a newer version of itself: " + root.plain(page.cygnus.last_error)
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: true
            type: Kirigami.MessageType.Information
            text: "System packages are updated together with your whole system (never partially). Cygnus checks the applications it manages against the source each one came from."
        }
        ProgressStrip {
            Layout.fillWidth: true
            running: page.checking
            value: page.progressValue
            text: page.progressLabel !== "" ? page.progressLabel : page.progressText
        }
        Kirigami.AbstractCard {
            Layout.fillWidth: true
            contentItem: Item {
                Layout.fillWidth: true
                implicitWidth: systemLayout.implicitWidth
                implicitHeight: systemLayout.implicitHeight
                RowLayout {
                    id: systemLayout
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    spacing: Kirigami.Units.largeSpacing
                    Kirigami.Icon {
                        source: "system-software-update"
                        Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                        Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            font.bold: true
                            text: "System packages"
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            text: !page.system ? "Not checked yet" : page.system.error ? "Could not check: " + page.system.error : page.system.packages.length ? page.system.packages.length + " updates available" + (page.system.kernel ? ", including a new kernel" : "") : "Up to date"
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            visible: !!(page.system && page.system.checked_at)
                            opacity: 0.7
                            text: page.system ? "Checked " + new Date(page.system.checked_at).toLocaleString(Qt.locale(), Locale.ShortFormat) + " · updated together, never partially" : ""
                        }
                    }
                    QQC2.Button {
                        text: "Update system…"
                        icon.name: "system-software-update"
                        enabled: !page.checking
                        onClicked: page.planUpgrade()
                    }
                }
            }
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: page.updates.length === 0
            text: "No managed applications"
        }
        Repeater {
            model: page.updates
            delegate: Kirigami.AbstractCard {
                Layout.fillWidth: true
                contentItem: Item {
                    Layout.fillWidth: true
                    implicitWidth: cardLayout.implicitWidth
                    implicitHeight: cardLayout.implicitHeight
                    RowLayout {
                        id: cardLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        spacing: Kirigami.Units.largeSpacing
                        Kirigami.Icon {
                            source: page.statusIcon(modelData)
                            Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: modelData.name + (modelData.current ? " " + modelData.current : "")
                                font.bold: true
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: page.statusText(modelData) + "  ·  " + page.describe(modelData.provider)
                                wrapMode: Text.Wrap
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                visible: modelData.status !== "up-to-date" && modelData.status !== "system" && modelData.detail !== ""
                                text: modelData.detail
                                wrapMode: Text.Wrap
                                opacity: 0.7
                            }
                        }
                        QQC2.Button {
                            visible: modelData.status === "update-available" && (modelData.format === "appimage" || modelData.format === "flatpak")
                            enabled: !page.checking
                            text: "Update"
                            icon.name: "update-none"
                            onClicked: page.applyUpdate(modelData)
                        }
                        QQC2.Button {
                            visible: modelData.status === "update-available" && !!modelData.facts && modelData.facts.kind === "converted"
                            enabled: !page.checking
                            text: "Download and review…"
                            icon.name: "download"
                            onClicked: page.fetchUpdate(modelData)
                        }
                    }
                }
            }
        }
    }

    Component.onCompleted: load()

    Kirigami.Dialog {
        id: upgradeDialog
        title: "Update the system"
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 34
        customFooterActions: [
            Kirigami.Action {
                objectName: "updateAction"
                text: "Update"
                icon.name: "system-software-update"
                enabled: !!(page.upgradePlan && (page.upgradePlan.upgrades.length || page.upgradePlan.installs.length || page.upgradePlan.removals.length)) && (!page.newsNeedsAck() || page.newsRead)
                onTriggered: {
                    upgradeDialog.close();
                    page.runUpgrade();
                }
            }
        ]
        ColumnLayout {
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: !!(page.upgradePlan && page.upgradePlan.snapshot)
                text: page.upgradePlan && page.upgradePlan.snapshot ? "Snapshot: " + page.upgradePlan.snapshot.text : ""
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: !!(page.upgradePlan && page.upgradePlan.news && !page.upgradePlan.news.ok)
                text: page.upgradePlan && page.upgradePlan.news && !page.upgradePlan.news.ok ? "Arch news could not be checked (" + page.upgradePlan.news.error + "). You can still update; if you can, read archlinux.org/news first." : ""
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font.bold: true
                visible: !!(page.upgradePlan && page.upgradePlan.news && page.upgradePlan.news.ok && page.upgradePlan.news.items.length)
                text: !page.upgradePlan || !page.upgradePlan.news || !page.upgradePlan.news.ok ? "" : "Arch news since your last update (" + page.upgradePlan.news.total + ")"
            }
            Repeater {
                model: page.upgradePlan && page.upgradePlan.news && page.upgradePlan.news.ok ? page.upgradePlan.news.items : []
                delegate: ColumnLayout {
                    Layout.fillWidth: true
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        font.bold: true
                        color: modelData.manual ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                        text: (modelData.manual ? "Needs a manual step: " : "") + modelData.title
                    }
                    QQC2.TextArea {
                        textFormat: TextEdit.PlainText
                        Layout.fillWidth: true
                        readOnly: true
                        wrapMode: TextEdit.Wrap
                        text: modelData.summary
                    }
                    QQC2.Button {
                        visible: modelData.link !== ""
                        text: "Read it on archlinux.org"
                        icon.name: "internet-services"
                        onClicked: Qt.openUrlExternally(modelData.link)
                    }
                }
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: !!(page.upgradePlan && page.upgradePlan.news && page.upgradePlan.news.ok && page.upgradePlan.news.total > page.upgradePlan.news.items.length)
                text: !page.upgradePlan || !page.upgradePlan.news || !page.upgradePlan.news.ok ? "" : "… and " + (page.upgradePlan.news.total - page.upgradePlan.news.items.length) + " more since your last update, none of them needing a manual step: archlinux.org/news"
            }
            QQC2.CheckBox {
                visible: page.newsNeedsAck()
                text: "I have read the news that needs a manual step, and I know what to do"
                checked: page.newsRead
                onToggled: page.newsRead = checked
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: page.upgradePlan ? root.flat(page.upgradePlan.upgrades.length || page.upgradePlan.installs.length || page.upgradePlan.removals.length ? page.upgradePlan.message + (page.upgradePlan.download_bytes ? " Download: " + Math.round(page.upgradePlan.download_bytes / 1048576) + " MiB." : "") : "The system is up to date.") : ""
            }
            QQC2.TextArea {
                textFormat: TextEdit.PlainText
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 16
                readOnly: true
                font.family: "monospace"
                visible: !!(page.upgradePlan && (page.upgradePlan.upgrades.length || page.upgradePlan.removals.length))
                text: page.upgradePlan ? page.upgradePlan.upgrades.map(u => u.name + "  " + u.from + " → " + u.to).concat(page.upgradePlan.installs.map(u => u.name + "  (new) " + u.to + (u.reason ? "  (" + u.reason + ")" : ""))).concat(page.upgradePlan.removals.map(u => u.name + "  removed: " + u.reason)).join("\n") : ""
            }
        }
    }
}
