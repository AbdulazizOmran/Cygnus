import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Discover: browse the programs you can get, from Flathub (and any other Flatpak remote you have), from your own repositories and from the
// vendors that publish a .deb or .rpm. Nothing is installed from here: opening a program shows every way to get it, and each way goes through
// the page that already exists for it, with its own look-before-you-confirm.
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
    title: qsTr("Discover")

    property var overview: null // what the lists are made of: how many programs, which data was read and how old it is
    property bool reading: true // the first look reads the application lists, which takes a few seconds
    property var results: []
    property int total: 0
    property var groups: [] // [{id, name, count}] among everything the words match
    property string group: ""
    property string kind: ""
    property bool searching: false
    property int searchSeq: 0 // a search answered after a newer one was asked is dropped
    property string errorText: "" // already made safe with root.plain()
    property alias query: searchField.text
    property string resultQuery: "" // what the list on screen was asked with: "Show more" continues THAT search, not whatever is typed since
    property string resultGroup: ""
    property string resultKind: ""

    readonly property var kinds: [
        {
            label: qsTr("From anywhere"),
            value: ""
        },
        {
            label: qsTr("Flatpak"),
            value: "flatpak"
        },
        {
            label: qsTr("Your repositories"),
            value: "repo"
        },
        {
            label: qsTr("A vendor's own package (.deb / .rpm)"),
            value: "vendor"
        }
    ]

    function search(more) {
        const seq = ++searchSeq;
        const offset = more ? results.length : 0;
        const asked = more ? {
            query: resultQuery,
            group: resultGroup,
            kind: resultKind
        } : {
            query: searchField.text.trim(),
            group: group,
            kind: kind
        };
        errorText = "";
        searching = true;
        page.ask(backend.discoverSearch(asked.query, asked.group, asked.kind, offset), function (r) {
            if (seq !== searchSeq)
                return;
            searching = false;
            if (!r.ok) {
                errorText = root.plain(r.error);
                if (!more)
                    results = [];
                return;
            }
            results = more ? results.concat(r.result.entries) : r.result.entries;
            resultQuery = asked.query;
            resultGroup = asked.group;
            resultKind = asked.kind;
            total = r.result.total;
            groups = r.result.groups;
        });
    }

    function load() {
        reading = true;
        page.ask(backend.discoverOverview(), function (r) {
            reading = false;
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            overview = r.result;
            page.search(false);
        });
    }

    function setGroup(id) {
        group = group === id ? "" : id; // choosing the one that is chosen clears it
        search(false);
    }

    function open(entry) {
        root.pageStack.push(discoverAppPage, {
            entryKey: entry.key
        });
    }

    function sourcesText(entry) {
        const names = {
            flatpak: qsTr("Flatpak"),
            repo: qsTr("Your repositories"),
            vendor: qsTr("Vendor's own package")
        };
        return entry.sources.map(s => names[s.kind] || s.kind).filter((n, i, all) => all.indexOf(n) === i).join(" · ");
    }

    function ageText(seconds) {
        if (!seconds)
            return "";
        const days = Math.floor((Date.now() / 1000 - seconds) / 86400);
        return days < 1 ? qsTr("today") : days === 1 ? qsTr("yesterday") : qsTr("%n days ago", "", days);
    }

    function dataNote() {
        if (!overview || !overview.sources.length)
            return "";
        return "Read from: " + overview.sources.map(s => s.label + (s.updated ? " (" + ageText(s.updated) + ")" : "")).join(", ") + ". Flathub's list is refreshed by Flatpak, not by Cygnus.";
    }

    Timer {
        id: debounce
        interval: 350
        onTriggered: page.search(false)
    }

    header: QQC2.ToolBar {
        contentItem: ColumnLayout {
            spacing: Kirigami.Units.smallSpacing
            Kirigami.SearchField {
                id: searchField
                Layout.fillWidth: true
                placeholderText: qsTr("Search programs (name, what it does…)")
                enabled: !page.reading
                autoAccept: false // the field would otherwise "accept" at every key, which searches at once and defeats the wait below
                onTextChanged: debounce.restart()
                onAccepted: {
                    debounce.stop();
                    page.search(false);
                }
            }
            Flow {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                visible: page.groups.length > 0
                Repeater {
                    model: page.groups
                    QQC2.ToolButton {
                        required property var modelData
                        objectName: "group-" + modelData.id
                        text: modelData.name + "  " + modelData.count
                        highlighted: page.group === modelData.id
                        onClicked: page.setGroup(modelData.id)
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QQC2.Label {
                    textFormat: Text.PlainText
                    text: qsTr("Get it:")
                    opacity: 0.7
                }
                QQC2.ComboBox {
                    id: kindBox
                    objectName: "kindBox"
                    Layout.fillWidth: true
                    model: page.kinds.map(k => k.label)
                    onActivated: index => {
                        page.kind = page.kinds[index].value;
                        page.search(false);
                    }
                }
            }
        }
    }

    ListView {
        id: list
        objectName: "resultList"
        model: page.results
        clip: true
        reuseItems: true
        header: ColumnLayout {
            width: list.width
            spacing: Kirigami.Units.smallSpacing
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Error
                visible: page.errorText !== ""
                text: page.errorText
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: !!page.overview && page.overview.problems.length > 0
                text: !page.overview || !page.overview.problems.length ? "" : root.plain("Some application lists could not be read and are left out: " + page.overview.problems.join(" "))
            }
            Kirigami.InlineMessage {
                objectName: "repoMissing"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Information
                visible: !!page.overview && page.overview.repo_missing
                text: qsTr("Programs from your own repositories are not listed, because the data package archlinux-appstream-data is not installed (it holds no program, only the lists). Settings → Optional parts installs it.")
                actions: [
                    Kirigami.Action {
                        text: qsTr("Open Settings")
                        icon.name: "configure"
                        onTriggered: root.openPage("settings", {})
                    }
                ]
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: page.results.length > 0
                opacity: 0.7
                objectName: "resultCount"
                text: (page.total === 1 ? qsTr("%1 program").arg(page.total) : qsTr("%1 programs").arg(page.total)) + (page.searching ? qsTr(" · searching…") : "")
            }
        }
        footer: ColumnLayout {
            width: list.width
            QQC2.Button {
                objectName: "moreButton"
                Layout.alignment: Qt.AlignHCenter
                visible: page.results.length > 0 && page.results.length < page.total
                enabled: !page.searching
                text: qsTr("Show more (%1 left)").arg(page.total - page.results.length)
                onClicked: page.search(true)
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                wrapMode: Text.Wrap
                opacity: 0.6
                visible: text !== ""
                text: page.dataNote()
            }
        }

        QQC2.BusyIndicator {
            anchors.centerIn: parent
            running: page.reading
            visible: page.reading
        }
        Kirigami.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.gridUnit * 4
            visible: list.count === 0 && !page.searching && !page.reading
            icon.name: "system-search"
            text: searchField.text.trim() === "" && page.group === "" && page.kind === "" ? qsTr("Nothing to show") : qsTr("No program matches")
            explanation: searchField.text.trim() === "" && page.group === "" && page.kind === "" ? qsTr("The application lists could not be read.") : qsTr("Check the spelling, try fewer words, or clear the filters. A program that is only in the AUR, or that comes as a file you download yourself, is installed from the Install page.")
        }

        delegate: QQC2.ItemDelegate {
            required property var modelData
            width: ListView.view.width
            onClicked: page.open(modelData)
            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing
                Kirigami.Icon {
                    source: modelData.icon_url !== "" ? modelData.icon_url : modelData.icon_name
                    fallback: "package-x-generic"
                    Layout.preferredWidth: Kirigami.Units.iconSizes.large
                    Layout.preferredHeight: Kirigami.Units.iconSizes.large
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        font.bold: true
                        text: modelData.name + (modelData.developer ? "  ·  " + modelData.developer : "")
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        opacity: 0.7
                        text: modelData.summary
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        opacity: 0.55
                        text: page.sourcesText(modelData)
                    }
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: modelData.installed
                    opacity: 0.8
                    text: qsTr("Installed")
                }
            }
        }
    }

    actions: [
        Kirigami.Action {
            text: qsTr("Reload")
            icon.name: "view-refresh"
            enabled: !page.reading
            onTriggered: page.load()
        }
    ]

    Component.onCompleted: load()
}
