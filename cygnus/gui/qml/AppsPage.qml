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
        } : undefined);
    }
    title: "Applications"

    property var apps: JSON.parse(backend ? backend.appsJson : "[]")
    // Applications Cygnus knows how to check even before they are managed (curated manifests).
    readonly property var known: [
        {
            name: "WhatPulse",
            icon: "input-keyboard",
            note: "Keyboard, mouse and network statistics"
        },
        {
            name: "Helium",
            icon: "internet-web-browser",
            note: "Web browser"
        }
    ]

    // Operations that stopped half-way (Cygnus closed, power loss): offered to finish or undo.
    property var interrupted: []
    property var staleLock: null // pacman's lock left behind by a crashed package manager
    property var lockPlan: null
    property string recoveryText: ""

    function loadInterrupted() {
        page.ask(backend.interruptedOperations(), function (r) {
            if (r.ok)
                page.interrupted = r.result;
        });
        page.ask(backend.stalePacmanLock(), function (r) {
            if (r.ok)
                page.staleLock = r.result;
        });
    }

    function removeStaleLock() {
        page.ask(backend.planClearStaleLock(), function (r) {
            if (!r.ok) {
                recoveryError.text = root.plain(r.error);
                recoveryError.visible = true;
                page.loadInterrupted();
                return;
            }
            page.lockPlan = r.result;
            lockDialog.open();
        });
    }

    function recover(op, action) {
        recoveryText = action === "resume" ? "Finishing…" : "Undoing…";
        page.ask(backend.recover(op.id, action), function (r) {
            page.recoveryText = "";
            if (!r.ok || !r.result.ok) {
                recoveryError.text = root.plain(root.why(r));
                recoveryError.visible = true;
            }
            page.loadInterrupted();
        }, function (line) {
            page.recoveryText = line;
        });
    }

    actions: [
        Kirigami.Action {
            text: "Install…"
            icon.name: "list-add"
            onTriggered: root.showPage(installPage)
        },
        Kirigami.Action {
            text: "Refresh"
            icon.name: "view-refresh"
            onTriggered: backend.refreshApps()
        }
    ]

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            id: recoveryError
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            showCloseButton: true
            visible: false
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: !!page.staleLock
            type: Kirigami.MessageType.Warning
            text: page.staleLock ? "<b>pacman is locked, but no package manager is running.</b> A package manager stopped unexpectedly " + page.staleLock.minutes + " minutes ago and left its lock behind; installs and updates fail until it is removed." : ""
            actions: [
                Kirigami.Action {
                    text: "Remove it…"
                    icon.name: "object-unlocked"
                    onTriggered: page.removeStaleLock()
                }
            ]
        }
        Repeater {
            model: page.interrupted
            delegate: Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: true
                type: Kirigami.MessageType.Warning
                text: "<b>" + root.plain(modelData.title) + " did not finish.</b> " + root.plain(modelData.explanation) + " (" + root.plain(modelData.progress) + ")" + (page.recoveryText ? "<br>" + root.plain(page.recoveryText) : "")
                actions: [
                    Kirigami.Action {
                        text: "Finish it"
                        icon.name: "media-playback-start"
                        enabled: page.recoveryText === ""
                        onTriggered: page.recover(modelData, "resume")
                    },
                    Kirigami.Action {
                        text: "Undo it"
                        icon.name: "edit-undo"
                        enabled: page.recoveryText === ""
                        onTriggered: page.recover(modelData, "rollback")
                    }
                ]
            }
        }

        Kirigami.Heading {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            level: 3
            text: "Managed by Cygnus"
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: page.apps.length === 0
            icon.name: "system-software-install"
            text: "No applications are managed yet"
            explanation: "Install an AppImage, a Flatpak bundle or a package — or adopt one you already have."
        }
        Repeater {
            model: page.apps
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
                            source: modelData.icon
                            fallback: "application-x-executable"
                            Layout.preferredWidth: Kirigami.Units.iconSizes.large
                            Layout.preferredHeight: Kirigami.Units.iconSizes.large
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Kirigami.Heading {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                level: 3
                                text: modelData.name
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: [modelData.format, modelData.version, modelData.location ? "on " + modelData.location : ""].filter(x => x).join(" · ")
                                opacity: 0.7
                            }
                        }
                        QQC2.Button {
                            text: "Details"
                            onClicked: root.pageStack.push(appPage, {
                                appName: modelData.name,
                                installationId: modelData.installation_id,
                                installationFormat: modelData.format,
                                installationLocation: modelData.location
                            })
                        }
                    }
                }
            }
        }

        Kirigami.Heading {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            level: 3
            text: "Check an application"
        }
        Repeater {
            model: page.known
            delegate: Kirigami.AbstractCard {
                Layout.fillWidth: true
                contentItem: Item {
                    Layout.fillWidth: true
                    implicitWidth: installedCardLayout.implicitWidth
                    implicitHeight: installedCardLayout.implicitHeight
                    RowLayout {
                        id: installedCardLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        Kirigami.Icon {
                            source: modelData.icon
                            Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: modelData.name
                                font.bold: true
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: modelData.note
                                opacity: 0.7
                            }
                        }
                        QQC2.Button {
                            text: "Check health"
                            icon.name: "checkmark"
                            onClicked: root.pageStack.push(appPage, {
                                appName: modelData.name
                            })
                        }
                    }
                }
            }
        }
    }

    Component.onCompleted: {
        backend.refreshApps();
        loadInterrupted();
    }

    Kirigami.PromptDialog {
        id: lockDialog
        title: "Remove pacman's lock?"
        subtitle: page.lockPlan ? root.flat(page.lockPlan.message) : ""
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Remove"
                icon.name: "object-unlocked"
                onTriggered: {
                    lockDialog.close();
                    if (page.lockPlan === null) // a second click while the dialog closes
                        return;
                    const plan = page.lockPlan;
                    page.lockPlan = null;
                    page.ask(backend.commitFix(plan.token), function (r) {
                        if (!r.ok || !r.result.ok) {
                            recoveryError.text = root.plain(root.why(r));
                            recoveryError.visible = true;
                        }
                        page.loadInterrupted();
                    });
                }
            }
        ]
    }
}
