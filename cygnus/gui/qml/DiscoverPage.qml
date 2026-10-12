import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Discover: browse the programs you can get, from Flathub (and any other Flatpak remote you have), from your own repositories and from the
// vendors that publish a .deb or .rpm, and from the makers' own download pages (AppImage, .deb, .rpm, archive). Nothing is installed from here: opening a program shows every way to get it, and each way goes through
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
    property var aurResult: null // the AUR's answer for the words in the box, when the person asked for it
    property bool aurBusy: false
    property int aurSeq: 0 // an answer for words that were changed since is dropped
    property string aurError: "" // already made safe with root.plain()
    property bool refreshing: false // the online catalogues are being downloaded
    property string refreshText: ""
    property string refreshError: "" // already made safe with root.plain()
    property bool refreshAgain: false
    property int loadSeq: 0 // an overview that arrives after a newer one was asked for is dropped

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
        },
        {
            label: qsTr("The maker's own download (AppImage, .deb, .rpm, archive, Arch package)"),
            value: "release"
        },
        {
            label: qsTr("Community AppImages (AppImageHub)"),
            value: "appimage"
        },
        {
            label: qsTr("Debian packages (converted, may not work)"),
            value: "debian"
        },
        {
            label: qsTr("Fedora packages (converted, may not work)"),
            value: "fedora"
        }
    ]

    function search(more) {
        const seq = ++searchSeq;
        const offset = more ? results.length : 0;
        if (!more) {
            aurSeq++; // the AUR answer on screen (or on its way) belongs to the words that were typed before
            aurResult = null;
            aurError = "";
            aurBusy = false;
        }
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
            if (!more)
                Qt.callLater(page.showTop); // a new first page starts at the top
            resultQuery = asked.query;
            resultGroup = asked.group;
            resultKind = asked.kind;
            total = r.result.total;
            groups = r.result.groups;
        });
    }

    // The AUR is not listed (it has no program lists): it is asked when you press the button, with the words in the box, and each answer is a package
    // that the Install page reviews and builds.
    function searchAur() {
        const words = searchField.text.trim();
        if (aurBusy || words.length < 2)
            return;
        const seq = ++aurSeq;
        aurBusy = true;
        aurError = "";
        aurResult = null;
        page.ask(backend.discoverAur(words), function (r) {
            if (seq !== aurSeq)
                return;
            aurBusy = false;
            if (!r.ok) {
                aurError = root.plain(r.error);
                return;
            }
            aurResult = r.result;
        });
    }

    // The online catalogues (AppImageHub...) are downloaded when they are switched on and missing or a week old: once for each opening of this page,
    // and again only when you press the button. Searching and browsing only read what was downloaded.
    function refreshCatalogues(force) {
        if (refreshing) {
            refreshAgain = true; // a catalogue switched on, or the button pressed, while one is downloading: asked again when that one is done
            return;
        }
        refreshing = true;
        refreshText = "";
        refreshError = "";
        page.ask(backend.catalogueRefresh(force), function (r) {
            refreshing = false;
            refreshText = "";
            if (!r.ok) {
                refreshError = root.plain(r.error);
                return;
            }
            refreshError = root.plain(Object.values(r.result.errors).join(" "));
            if (r.result.refreshed.length > 0 || r.result.busy)
                page.load(false); // the lists have more in them now
            if (r.result.busy)
                retryRefresh.restart(); // another download is still running (an earlier page's): ask again in a moment
            if (refreshAgain) {
                refreshAgain = false;
                page.refreshCatalogues(false);
            }
        }, function (line) {
            if (line !== "")
                page.refreshText = line;
        });
    }

    // The one question asked before anything is downloaded by itself: yes downloads the catalogues now; not now switches them all off (they can be switched on
    // one by one below).
    function decideCatalogues(accept) {
        page.ask(backend.catalogueDecide(accept), function (r) {
            if (!r.ok) {
                refreshError = root.plain(r.error);
                return;
            }
            page.load(accept);
        });
    }

    function setCatalogue(id, on) {
        page.ask(backend.catalogueSetOn(id, on), function (r) {
            if (!r.ok) {
                refreshError = root.plain(r.error);
                page.load(false); // the switch shows what is saved, not the position it was clicked to
                return;
            }
            page.load(false);
            if (on)
                page.refreshCatalogues(false);
        });
    }

    function load(withCatalogues) {
        reading = true;
        const seq = ++loadSeq;
        page.ask(backend.discoverOverview(), function (r) {
            if (seq !== loadSeq)
                return;
            reading = false;
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            overview = r.result;
            Qt.callLater(page.keepTop); // a message above the list (the first-run question) must not push itself out of sight
            page.search(false);
            if (withCatalogues && overview.catalogues_decided && overview.catalogues.some(c => c.on && c.stale))
                page.refreshCatalogues(false);
        });
    }

    function showTop() {
        list.positionViewAtBeginning();
    }

    // The header above the list grows when a message appears; the list then keeps the first program where it was and the message ends up above the
    // visible part. Near the top, stay at the top.
    function keepTop() {
        if (list.contentY < list.originY + Kirigami.Units.gridUnit * 12)
            list.positionViewAtBeginning();
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
            vendor: qsTr("Vendor's own package"),
            release: qsTr("Maker's own download"),
            appimage: qsTr("Community AppImage"),
            debian: qsTr("Debian package (may not work)"),
            fedora: qsTr("Fedora package (may not work)")
        };
        return entry.sources.map(s => names[s.kind] || s.kind).filter((n, i, all) => all.indexOf(n) === i).join(" · ");
    }

    // A program that is only in a list somebody else keeps says so next to its name, not only in the small grey line: nobody has checked a community
    // AppImage, and a package built for another system may not work here.
    function badgeText(entry) {
        const kinds = entry.sources.map(s => s.kind);
        if (kinds.every(k => k === "appimage"))
            return qsTr("Not checked");
        if (kinds.every(k => k === "debian" || k === "fedora"))
            return qsTr("May not work");
        return "";
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
        return root.fill(qsTr("Read from: %1. Flathub's list is refreshed by Flatpak, not by Cygnus."), [overview.sources.map(s => s.label + (s.updated ? " (" + ageText(s.updated) + ")" : "")).join(", ")]);
    }

    Timer {
        id: retryRefresh
        interval: 5000
        onTriggered: page.refreshCatalogues(false)
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
            onHeightChanged: page.keepTop() // a message appeared or went away above the list: stay at the top if that is where it was
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
                objectName: "cataloguesQuestion"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Information
                visible: !!page.overview && !page.overview.catalogues_decided
                text: qsTr("Discover can also list programs from other places: AppImageHub (community AppImages), Debian and Fedora (their packages, which Cygnus tries to convert and which may not work). That downloads about 50 MB now and again each week, from appimage.github.io, deb.debian.org, dl.fedoraproject.org and aur.archlinux.org. Nothing is installed by it. Debian's and Fedora's programs are left out when the Arch repositories, the AUR or Flathub already have them, and AppImageHub's when the Arch repositories, Flathub or a maker's own download already have them.")
                actions: [
                    Kirigami.Action {
                        objectName: "cataloguesYes"
                        text: qsTr("Download and list them")
                        icon.name: "download"
                        onTriggered: page.decideCatalogues(true)
                    },
                    Kirigami.Action {
                        objectName: "cataloguesNo"
                        text: qsTr("Not now")
                        onTriggered: page.decideCatalogues(false)
                    }
                ]
            }
            Kirigami.InlineMessage {
                objectName: "refreshingMessage"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Information
                visible: page.refreshing
                text: page.refreshText !== "" ? root.plain(page.refreshText) : qsTr("Updating the online catalogues…")
            }
            Kirigami.InlineMessage {
                objectName: "refreshError"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: page.refreshError !== ""
                text: page.refreshError
            }
            Kirigami.InlineMessage {
                objectName: "repoMissing"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Information
                visible: !!page.overview && page.overview.repo_missing
                text: qsTr("Programs from your own repositories are not listed, because the data package archlinux-appstream-data is not installed (it holds no program, only the lists, and Cygnus's own package normally brings it). Settings → Optional parts installs it.")
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
            QQC2.Button {
                objectName: "aurSearchButton"
                Layout.alignment: Qt.AlignHCenter
                visible: !page.reading && searchField.text.trim().length >= 2
                enabled: !page.aurBusy
                icon.name: "edit-find"
                text: qsTr("Also search the AUR for “%1”").arg(searchField.text.trim())
                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.text: qsTr("The AUR is the Arch User Repository: packages that volunteers write, built on your computer. Your words are sent to aur.archlinux.org.")
                onClicked: page.searchAur()
            }
            QQC2.BusyIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: page.aurBusy
                visible: page.aurBusy
            }
            Kirigami.InlineMessage {
                objectName: "aurError"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Error
                visible: page.aurError !== ""
                text: page.aurError
            }
            Kirigami.Heading {
                objectName: "aurHeading"
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                wrapMode: Text.Wrap
                level: 3
                visible: !!page.aurResult
                text: !page.aurResult ? "" : page.aurResult.total === 0 ? qsTr("Nothing in the AUR matches these words") : page.aurResult.total > page.aurResult.entries.length ? qsTr("From the AUR: the %1 most popular of %2 packages").arg(page.aurResult.entries.length).arg(page.aurResult.total) : qsTr("From the AUR: %1 packages").arg(page.aurResult.total)
            }
            Kirigami.InlineMessage {
                objectName: "aurWarning"
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: !!page.aurResult && page.aurResult.entries.length > 0
                text: qsTr("Anyone can upload to the AUR, and a package is a set of instructions that is run on your computer to build it. Votes and popularity do not make one safe. Cygnus shows you the build script and what it needs before anything is built.")
            }
            Repeater {
                model: page.aurResult ? page.aurResult.entries : []
                Kirigami.AbstractCard {
                    id: aurCard
                    required property var modelData
                    Layout.fillWidth: true
                    contentItem: ColumnLayout {
                        spacing: Kirigami.Units.smallSpacing
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.bold: true
                            text: aurCard.modelData.name + (aurCard.modelData.version !== "" ? "  " + aurCard.modelData.version : "") + (aurCard.modelData.installed ? qsTr("  ·  installed") : "")
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            visible: aurCard.modelData.summary !== ""
                            text: aurCard.modelData.summary
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            opacity: 0.7
                            text: qsTr("%1 votes").arg(aurCard.modelData.votes) + "  ·  " + (aurCard.modelData.maintainer !== "" ? qsTr("looked after by %1").arg(aurCard.modelData.maintainer) : qsTr("nobody looks after it (orphaned)")) + (aurCard.modelData.out_of_date ? "  ·  " + qsTr("flagged out of date") : "")
                        }
                        RowLayout {
                            spacing: Kirigami.Units.smallSpacing
                            QQC2.Button {
                                objectName: "aurInstallButton"
                                icon.name: "package-x-generic"
                                text: aurCard.modelData.installed ? qsTr("Look at it again…") : qsTr("Review and install…")
                                onClicked: root.openPage("install", {
                                    "appSpec": `aur:${aurCard.modelData.name}`
                                })
                            }
                            QQC2.Button {
                                objectName: "aurWebsiteButton"
                                visible: aurCard.modelData.url !== ""
                                icon.name: "internet-services"
                                text: qsTr("Website")
                                onClicked: Qt.openUrlExternally(aurCard.modelData.url)
                            }
                            Item {
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }
            Kirigami.Heading {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.largeSpacing
                level: 4
                visible: !!page.overview && page.overview.catalogues_decided && page.overview.catalogues.length > 0
                text: qsTr("Online catalogues")
            }
            Repeater {
                model: page.overview && page.overview.catalogues_decided ? page.overview.catalogues : []
                RowLayout {
                    id: catalogueRow
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    spacing: Kirigami.Units.largeSpacing
                    QQC2.Switch {
                        objectName: "catalogueSwitch"
                        checked: catalogueRow.modelData.on
                        onToggled: page.setCatalogue(catalogueRow.modelData.id, checked)
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        QQC2.Label {
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.bold: true
                            text: catalogueRow.modelData.label
                        }
                        QQC2.Label {
                            objectName: "catalogueState"
                            textFormat: Text.PlainText
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            opacity: 0.7
                            text: catalogueRow.modelData.what + " " + (catalogueRow.modelData.count > 0 ? qsTr("%1 programs, downloaded %2.").arg(catalogueRow.modelData.count).arg(page.ageText(catalogueRow.modelData.fetched)) : qsTr("Not downloaded yet (%1).").arg(catalogueRow.modelData.size)) + (catalogueRow.modelData.on ? "" : " " + qsTr("Switched off: it is not listed and not downloaded."))
                        }
                    }
                }
            }
            QQC2.Button {
                objectName: "catalogueUpdateButton"
                Layout.alignment: Qt.AlignHCenter
                visible: !!page.overview && page.overview.catalogues_decided && page.overview.catalogues.some(c => c.on)
                enabled: !page.refreshing
                icon.name: "view-refresh"
                text: qsTr("Update the online catalogues now")
                onClicked: page.refreshCatalogues(true)
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
            explanation: searchField.text.trim() === "" && page.group === "" && page.kind === "" ? qsTr("The application lists could not be read.") : qsTr("Check the spelling, try fewer words, or clear the filters. A program that is only in the AUR can be searched for with the button below; one that comes as a file you download yourself is installed from the Install page.")
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
                    objectName: "listBadge"
                    textFormat: Text.PlainText
                    visible: page.badgeText(modelData) !== ""
                    font.bold: true
                    color: Kirigami.Theme.neutralTextColor
                    text: page.badgeText(modelData)
                }
                QQC2.Label {
                    objectName: "listInstalled"
                    textFormat: Text.PlainText
                    visible: modelData.installed
                    opacity: 0.8
                    text: modelData.installed_hint ? qsTr("Same name installed") : qsTr("Installed")
                }
            }
        }
    }

    actions: [
        Kirigami.Action {
            text: qsTr("Reload")
            icon.name: "view-refresh"
            enabled: !page.reading
            onTriggered: page.load(false)
        }
    ]

    Component.onCompleted: load(true)
}
