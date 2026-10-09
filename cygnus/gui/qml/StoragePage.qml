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
    title: "Storage"

    property var storage: JSON.parse(backend ? backend.storageJson : "{\"locations\": [], \"candidates\": []}")
    property string addPath: ""

    // null = not known; 0 is a full drive and says so. Small amounts keep a decimal so 200 MiB does not read "0 GiB".
    function gib(n) {
        if (n === null || n === undefined)
            return "?";
        const g = n / 1073741824;
        return (g < 10 ? g.toFixed(1) : g.toFixed(0)) + " GiB";
    }

    actions: [
        Kirigami.Action {
            text: "Refresh"
            icon.name: "view-refresh"
            onTriggered: backend.refreshStorage()
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

        Kirigami.Heading {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            level: 3
            text: "Application storage"
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: page.storage.locations.length === 0
            icon.name: "drive-harddisk"
            text: "No storage locations yet"
            explanation: "Choose below where applications may be stored, for example your SSD and your HDD."
        }
        Repeater {
            model: page.storage.locations
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
                        Kirigami.Icon {
                            source: modelData.online ? "drive-harddisk" : "drive-harddisk-error"
                            Layout.preferredWidth: Kirigami.Units.iconSizes.large
                            Layout.preferredHeight: Kirigami.Units.iconSizes.large
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Kirigami.Heading {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                level: 3
                                text: modelData.label + (modelData.default ? "  (default)" : "")
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                text: modelData.online ? "Applications folder: " + modelData.apps_dir : "Unavailable: " + modelData.reason
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            QQC2.Label {
                                textFormat: Text.PlainText
                                text: "Can store: apps you run" + (modelData.flatpak ? ", Flatpak apps" : "") + (modelData.class === "system" ? ", system packages" : "  ·  Cannot store: system packages")
                                opacity: 0.7
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }
        }

        Kirigami.Heading {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            level: 3
            text: "Drives on this computer"
        }
        Repeater {
            model: page.storage.candidates
            delegate: Kirigami.AbstractCard {
                Layout.fillWidth: true
                contentItem: Item {
                    Layout.fillWidth: true
                    implicitWidth: locationCardLayout.implicitWidth
                    implicitHeight: locationCardLayout.implicitHeight
                    ColumnLayout {
                        id: locationCardLayout
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        RowLayout {
                            Kirigami.Icon {
                                source: modelData.rotational ? "drive-harddisk" : "drive-harddisk-solidstate"
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
                                    text: modelData.mount + "  ·  " + page.gib(modelData.free) + " free of " + page.gib(modelData.size)
                                    opacity: 0.7
                                }
                            }
                            QQC2.Button {
                                text: "Use for applications…"
                                icon.name: "list-add"
                                enabled: modelData.eligible
                                onClicked: {
                                    page.addPath = modelData.mount;
                                    labelField.text = modelData.rotational ? "HDD" : "SSD";
                                    addDialog.open();
                                }
                            }
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            visible: !modelData.eligible
                            text: modelData.reason
                            color: Kirigami.Theme.negativeTextColor
                        }
                        Repeater {
                            model: modelData.notes
                            delegate: QQC2.Label {
                                textFormat: Text.PlainText
                                text: "• " + modelData
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                opacity: 0.8
                            }
                        }
                    }
                }
            }
        }
    }

    Kirigami.PromptDialog {
        id: addDialog
        title: root.flat("Store applications on " + page.addPath)
        subtitle: "Cygnus will test what this drive supports (it creates and removes a small test folder)."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Add"
                icon.name: "dialog-ok"
                onTriggered: {
                    addDialog.close();
                    page.ask(backend.addLocation(page.addPath, labelField.text, defaultBox.checked), function (r) {
                        if (!r.ok) {
                            errorMessage.text = root.plain(r.error);
                            errorMessage.visible = true;
                        }
                    });
                }
            }
        ]
        ColumnLayout {
            QQC2.TextField {
                id: labelField
                placeholderText: "Name, e.g. HDD"
            }
            QQC2.CheckBox {
                id: defaultBox
                text: "Use as the default location"
            }
        }
    }

    Component.onCompleted: backend.refreshStorage()
}
