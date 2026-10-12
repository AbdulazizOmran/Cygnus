import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// One program from Discover, with every way to get it. Each way is only a hand-over: it opens the page that already does that job (the
// Install page for a Flatpak or a vendor's file, the Packages page for a repository package), where the usual analysis and confirmation
// happen. Nothing is installed from here.
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
            if (!alive.gone)
                page.progressValue = value;
        } : undefined);
    }
    title: entry ? root.flat(entry.name) : qsTr("Program")

    property string entryKey: ""
    property var entry: null
    property bool loading: true
    property string busyText: ""
    property real progressValue: -1 // how far a download really is (0..1); -1 = not known
    property string errorText: "" // already made safe with root.plain()

    function load() {
        loading = true;
        errorText = "";
        page.ask(backend.discoverApp(entryKey), function (r) {
            loading = false;
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            entry = r.result;
        });
    }

    // A Flatpak: the Install page analyses it (what it needs, where it goes) and asks before anything is installed.
    function installFlatpak(source) {
        root.openPage("install", {
            "appSpec": source.remote + ":" + source.id
        });
    }

    // A repository package: its page in Packages (what it needs, what needs it, Install with the usual confirmation).
    function openPackage(source) {
        root.pageStack.push(packagePage, {
            packageName: source.package,
            repo: ""
        });
    }

    // A vendor's own file: fetched over HTTPS (checked against the checksum the vendor publishes, where it publishes one), then opened on the
    // Install page like any file you chose. Nothing is converted or installed until you confirm there.
    function fetchVendor(source, format) {
        if (busyText !== "")
            return;
        errorText = "";
        progressValue = -1;
        busyText = qsTr("Looking for the newest version…");
        page.ask(backend.fetchVendorPackage(source.package, format), function (r) {
            busyText = "";
            if (!r.ok || !r.result.ok) {
                errorText = root.plain(root.why(r));
                return;
            }
            root.openPage("install", {
                "fileUrl": r.result.file_url,
                "launchNotice": root.fill(qsTr("Cygnus downloaded the vendor's .%1 for %2 (version %3) and %4. Look at what it would do below; nothing is installed until you confirm."), [r.result.format, root.flat(page.entry.name), root.flat(r.result.version), r.result.checked ? qsTr("checked it against the checksum the vendor publishes") : qsTr("could not check it against a checksum, because the vendor publishes none for this file (the connection was secure, and that is all that vouches for it)")])
            });
        }, function (line) {
            if (line !== "")
                page.busyText = line;
        });
    }

    function kindText(source) {
        switch (source.kind) {
        case "flatpak":
            return qsTr("Runs in a sandbox of its own and updates through Flatpak. Cygnus looks at what it needs and where it will go before it installs anything.");
        case "repo":
            return qsTr("Part of your system: installed and updated by pacman together with everything else, and it uses the libraries your system already has.");
        default:
            return qsTr("The maker's own package, which Cygnus converts into a pacman package when that is safe (it shows you what the vendor's install scripts would have done). It is not sandboxed, and it does not update itself: Cygnus tells you about newer versions and you download and review them.");
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            visible: page.errorText !== "" // no close button: closing one removes this binding, and the next error would never show
            text: page.errorText
        }
        QQC2.BusyIndicator {
            Layout.alignment: Qt.AlignHCenter
            running: page.loading
            visible: page.loading
        }
        ProgressStrip {
            Layout.fillWidth: true
            running: page.busyText !== ""
            value: page.progressValue
            text: page.busyText
        }

        RowLayout {
            Layout.fillWidth: true
            visible: !!page.entry
            spacing: Kirigami.Units.largeSpacing
            Kirigami.Icon {
                source: !page.entry ? "" : page.entry.icon_url !== "" ? page.entry.icon_url : page.entry.icon_name
                fallback: "package-x-generic"
                Layout.preferredWidth: Kirigami.Units.iconSizes.huge
                Layout.preferredHeight: Kirigami.Units.iconSizes.huge
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Kirigami.Heading {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    level: 1
                    text: page.entry ? page.entry.name : ""
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    visible: !!page.entry && page.entry.developer !== ""
                    opacity: 0.7
                    text: page.entry ? page.entry.developer : ""
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: page.entry ? page.entry.summary : ""
                }
            }
        }
        QQC2.Label {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            visible: !!page.entry && page.entry.description !== ""
            text: page.entry ? page.entry.description : ""
        }
        QQC2.Button {
            objectName: "websiteButton"
            visible: !!page.entry && page.entry.url.indexOf("https://") === 0
            text: qsTr("Website")
            icon.name: "internet-services"
            onClicked: Qt.openUrlExternally(page.entry.url)
        }

        Kirigami.Heading {
            textFormat: Text.PlainText
            visible: !!page.entry
            level: 2
            text: page.entry && page.entry.sources.length > 1 ? qsTr("Ways to get it") : qsTr("How to get it")
        }
        Repeater {
            model: page.entry ? page.entry.sources : []
            Kirigami.AbstractCard {
                required property var modelData
                Layout.fillWidth: true
                contentItem: ColumnLayout {
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        font.bold: true
                        text: modelData.label + (modelData.installed ? qsTr("  ·  installed") : "")
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        opacity: 0.8
                        text: page.kindText(modelData)
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        visible: modelData.kind === "vendor" && modelData.note !== ""
                        opacity: 0.8
                        text: modelData.note || ""
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        QQC2.Button {
                            objectName: "flatpakButton"
                            visible: modelData.kind === "flatpak"
                            enabled: page.busyText === ""
                            text: modelData.installed ? qsTr("Look at it again…") : qsTr("Install…")
                            icon.name: "list-add"
                            onClicked: page.installFlatpak(modelData)
                        }
                        QQC2.Button {
                            objectName: "repoButton"
                            visible: modelData.kind === "repo"
                            enabled: page.busyText === ""
                            text: qsTr("Show the package…")
                            icon.name: "package-x-generic"
                            onClicked: page.openPackage(modelData)
                        }
                        QQC2.Button {
                            objectName: "debButton"
                            visible: modelData.kind === "vendor" && modelData.formats.indexOf("deb") >= 0
                            enabled: page.busyText === ""
                            text: qsTr("Download the .deb and review…")
                            icon.name: "download"
                            QQC2.ToolTip.visible: hovered
                            QQC2.ToolTip.text: qsTr("The vendor publishes a checksum for this file, and Cygnus checks it.")
                            onClicked: page.fetchVendor(modelData, "deb")
                        }
                        QQC2.Button {
                            objectName: "rpmButton"
                            visible: modelData.kind === "vendor" && modelData.formats.indexOf("rpm") >= 0
                            enabled: page.busyText === ""
                            text: qsTr("Download the .rpm and review…")
                            icon.name: "download"
                            QQC2.ToolTip.visible: hovered
                            QQC2.ToolTip.text: qsTr("The vendor publishes no checksum for this file: only the secure connection vouches for it. Prefer the .deb when there is one.")
                            onClicked: page.fetchVendor(modelData, "rpm")
                        }
                        Item {
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }
    }

    Component.onCompleted: load()
}
