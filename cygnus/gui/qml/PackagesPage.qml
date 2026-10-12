import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Packages from your repositories, by name: search what is on offer, look at what is installed, open one for its details.
// Nothing is changed from here; the details page installs and removes (always through the usual confirmation).
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
    title: qsTr("Packages")

    property string mode: "available" // available = what the repositories offer (a search); installed = what is on this computer
    property var results: []
    property int total: 0
    property bool truncated: false
    property bool searching: false
    property int searchSeq: 0 // a search answered after a newer one was asked is dropped
    property var installedAll: []
    property int updatesPending: 0
    property var lists: null // when the package lists were last refreshed
    property bool loadingInstalled: false
    property int filterIndex: 0
    property string busyText: ""
    property alias query: searchField.text // what is typed in the search box
    property string errorText: "" // already made safe with root.plain()
    property string doneText: "" // already made safe with root.plain()
    property var picked: ({}) // "no longer needed" packages ticked for removal: name -> true
    readonly property int batchLimit: 200 // what the helper accepts in one removal (cygnus.core.packages.MAX_BATCH)
    property var confirmPlan: null // the plan whose window is open
    property var warnPlan: null // "Cygnus did not install these": the question asked before the removal is even planned
    property bool warnTicked: false
    property var cacheShown: null // what cleaning the package cache with the chosen settings would remove
    property int cacheSeq: 0 // a preview answered after a newer one was asked is dropped
    readonly property bool selecting: mode === "installed" && filterIndex === 3 // the "No longer needed" list, where packages can be ticked
    onSelectingChanged: picked = ({})

    readonly property var filters: [
        {
            label: qsTr("All installed packages"),
            test: p => true
        },
        {
            label: qsTr("Chosen by you"),
            test: p => p.reason === "explicit"
        },
        {
            label: qsTr("Installed for another package"),
            test: p => p.reason === "dependency"
        },
        {
            label: qsTr("No longer needed"),
            test: p => !!p.unneeded && !p.protected && !p.managed && !p.odd_name // what could be removed from here: never a protected package, an application of Cygnus's, or a name the helper does not take
        },
        {
            label: qsTr("Updates available"),
            test: p => !!p.update
        },
        {
            label: qsTr("From no repository (a file or the AUR)"),
            test: p => !p.repo
        }
    ]

    function installedShown() {
        const words = searchField.text.toLowerCase().split(/\s+/).filter(w => w !== "");
        const test = filters[filterIndex].test;
        return installedAll.filter(p => test(p) && words.every(w => p.name.toLowerCase().indexOf(w) >= 0 || p.desc.toLowerCase().indexOf(w) >= 0));
    }
    readonly property var shown: mode === "installed" ? installedShown() : results

    function search() {
        if (mode !== "available")
            return;
        const text = searchField.text.trim();
        const seq = ++searchSeq;
        errorText = "";
        if (text.length < 2) {
            results = [];
            total = 0;
            truncated = false;
            searching = false;
            return;
        }
        searching = true;
        page.ask(backend.packagesSearch(text), function (r) {
            if (seq !== searchSeq)
                return;
            searching = false;
            if (!r.ok) {
                results = [];
                errorText = root.plain(r.error);
                return;
            }
            results = r.result.results;
            total = r.result.total;
            truncated = r.result.truncated;
            lists = r.result.lists;
        });
    }

    function loadInstalled() {
        loadingInstalled = true;
        page.ask(backend.packagesInstalled(), function (r) {
            loadingInstalled = false;
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            installedAll = r.result.packages;
            updatesPending = r.result.updates;
            lists = r.result.lists;
            const still = {}; // a tick stays only for a package that is still there and still no longer needed (a cancelled change keeps them all)
            installedAll.forEach(p => {
                if (picked[p.name] && !!p.unneeded)
                    still[p.name] = true;
            });
            picked = still;
        });
    }

    function listsNote() {
        if (!lists || !lists.refreshed)
            return "";
        const hours = Math.floor((Date.now() / 1000 - lists.refreshed) / 3600);
        const ago = hours < 1 ? qsTr("less than an hour ago") : hours < 48 ? hours + " hours ago" : Math.floor(hours / 24) + " days ago";
        return "Package lists refreshed " + ago + ". Cygnus never refreshes them on their own (that can break a system): updating the whole system does.";
    }

    function sizeText(n) {
        return n >= 1048576 ? (n / 1048576).toFixed(1) + " MiB" : Math.max(1, Math.round(n / 1024)) + " KiB";
    }

    function statusOf(p) {
        if (p.update)
            return "Update available" + (p.update_to ? ": " + p.update_to : "");
        if (p.installed_version || mode === "installed")
            return "Installed";
        return "";
    }

    function open(p) {
        root.pageStack.push(packagePage, {
            packageName: p.name,
            repo: mode === "available" ? p.repo : ""
        });
    }

    function pick(name, on) {
        const next = Object.assign({}, picked);
        if (on)
            next[name] = true;
        else
            delete next[name];
        picked = next;
    }

    function pickAll() {
        const next = {};
        shown.forEach(p => next[p.name] = true);
        picked = next;
    }

    // What is ticked AND on the screen now: what the count says and what is sent are the same packages (a tick hidden by the search box
    // is not removed behind your back).
    function pickedNames() {
        return shown.filter(p => !!p.unneeded && !p.protected && !p.managed && !p.odd_name && !!picked[p.name]).map(p => p.name);
    }

    function planRemoveSelected(confirmedNotMine) {
        const names = pickedNames();
        if (!names.length || names.length > batchLimit || busyText !== "")
            return;
        errorText = "";
        doneText = "";
        busyText = qsTr("Preparing…");
        page.ask(backend.planRemovePackagesByName(JSON.stringify(names), confirmedNotMine), function (r) {
            busyText = "";
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            if (r.result.refused) { // packages Cygnus did not install: ask on purpose, then plan again
                warnPlan = r.result;
                warnTicked = false;
                warnDialog.open();
                return;
            }
            confirmPlan = r.result;
            confirmDialog.open();
        });
    }

    function openCache() {
        errorText = "";
        doneText = "";
        cacheShown = null;
        cacheDialog.open();
        previewCache();
    }

    function previewCache() {
        const seq = ++cacheSeq;
        cacheShown = null;
        page.ask(backend.cachePreview(cacheKeep.value, cacheUninstalled.checked), function (r) {
            if (seq !== cacheSeq)
                return;
            if (!r.ok) {
                errorText = root.plain(r.error);
                cacheDialog.close();
                return;
            }
            cacheShown = r.result;
        });
    }

    function planCache() {
        if (busyText !== "")
            return;
        errorText = "";
        doneText = "";
        busyText = qsTr("Preparing…");
        page.ask(backend.planCacheClean(cacheKeep.value, cacheUninstalled.checked), function (r) {
            busyText = "";
            if (!r.ok) {
                errorText = root.plain(r.error);
                return;
            }
            confirmPlan = r.result;
            confirmDialog.open();
        });
    }

    // The plan is handed in by the window's button: closing the window clears confirmPlan, and when that happens at once a plan read
    // afterwards would be gone and nothing would run.
    function runPlan(plan) {
        if (busyText !== "" || !plan)
            return;
        confirmPlan = null;
        busyText = qsTr("Waiting for authorization…");
        page.ask(backend.commitFix(plan.token), function (r) {
            busyText = "";
            if (!r.ok || !r.result.ok)
                errorText = root.plain(root.why(r));
            else
                doneText = root.plain(plan.cache ? qsTr("The package cache was cleaned.") : qsTr("Removed."));
            page.loadInstalled();
        }, function (line) {
            if (line.trim() !== "") // a blank line from pacman must not end "busy": that is what keeps a second change from starting
                page.busyText = line;
        });
    }

    // A change to the system's packages was made (from a package's page, even one that has been left since): what it installed or removed
    // shows at once.
    Connections {
        target: backend
        function onPackagesChanged() {
            page.loadInstalled();
            page.search();
        }
    }

    Timer {
        id: debounce
        interval: 350
        onTriggered: page.search()
    }

    header: QQC2.ToolBar {
        contentItem: ColumnLayout {
            spacing: Kirigami.Units.smallSpacing
            Kirigami.SearchField {
                id: searchField
                Layout.fillWidth: true
                placeholderText: page.mode === "available" ? qsTr("Search the repositories (name, description…)") : qsTr("Filter the installed packages")
                autoAccept: false // the field would otherwise "accept" at every key, which searches at once and defeats the wait below
                onTextChanged: debounce.restart()
                onAccepted: {
                    debounce.stop();
                    page.search();
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QQC2.TabBar {
                    id: modeBar
                    QQC2.TabButton {
                        text: qsTr("Available")
                    }
                    QQC2.TabButton {
                        text: qsTr("Installed")
                    }
                    onCurrentIndexChanged: {
                        page.mode = currentIndex === 0 ? "available" : "installed";
                        page.search();
                    }
                }
                QQC2.ComboBox {
                    Layout.fillWidth: true
                    visible: page.mode === "installed"
                    model: page.filters.map(f => f.label)
                    currentIndex: page.filterIndex
                    onActivated: index => page.filterIndex = index
                }
                Item {
                    Layout.fillWidth: true
                    visible: page.mode !== "installed"
                }
            }
            RowLayout {
                Layout.fillWidth: true
                visible: page.selecting
                QQC2.Button {
                    text: qsTr("Select all")
                    onClicked: page.pickAll()
                }
                QQC2.Button {
                    text: qsTr("Select none")
                    onClicked: page.picked = ({})
                }
                Item {
                    Layout.fillWidth: true
                }
                QQC2.Button {
                    text: page.pickedNames().length > page.batchLimit ? "Remove selected (" + page.pickedNames().length + "): at most " + page.batchLimit + " at a time" : "Remove selected (" + page.pickedNames().length + ")…"
                    icon.name: "edit-delete"
                    enabled: page.pickedNames().length > 0 && page.pickedNames().length <= page.batchLimit && page.busyText === ""
                    onClicked: page.planRemoveSelected(false)
                }
            }
        }
    }

    ListView {
        id: list
        model: page.shown
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
                type: Kirigami.MessageType.Positive
                visible: page.doneText !== ""
                text: page.doneText
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                visible: page.selecting
                opacity: 0.8
                text: qsTr("Installed for another package, and nothing needs them now. Look before you remove: a tool you use by hand, or built something with, can be here, and removing some can leave others that only they needed (they show up next time).")
            }
            QQC2.ProgressBar {
                Layout.fillWidth: true
                indeterminate: true
                visible: page.busyText !== ""
            }
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: page.updatesPending > 0
                type: Kirigami.MessageType.Warning
                text: page.updatesPending + (page.updatesPending === 1 ? qsTr(" update is") : qsTr(" updates are")) + " waiting. Update the whole system before you install anything new: installing on a system that is not up to date can break it."
                actions: [
                    Kirigami.Action {
                        text: qsTr("Open Updates")
                        icon.name: "update-none"
                        onTriggered: root.openPage("updates")
                    }
                ]
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                wrapMode: Text.Wrap
                opacity: 0.7
                visible: text !== ""
                text: page.mode === "available" && page.truncated ? page.total + " packages match; the first " + page.results.length + " are shown. Add more words to narrow it down." : page.mode === "available" && page.total > 0 ? page.total + " packages match." : page.mode === "installed" ? page.shown.length + " of " + page.installedAll.length + " installed packages." : ""
            }
            QQC2.ProgressBar {
                Layout.fillWidth: true
                indeterminate: true
                visible: page.searching || page.loadingInstalled
            }
        }
        footer: QQC2.Label {
            textFormat: Text.PlainText
            width: list.width
            padding: Kirigami.Units.largeSpacing
            wrapMode: Text.Wrap
            opacity: 0.7
            text: page.listsNote()
        }

        Kirigami.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.gridUnit * 4
            visible: list.count === 0 && !page.searching && !page.loadingInstalled
            icon.name: "package-x-generic"
            text: page.mode === "available" ? (searchField.text.trim().length < 2 ? qsTr("Search for a package") : qsTr("No package matches")) : qsTr("No installed package matches")
            explanation: page.mode === "available" ? (searchField.text.trim().length < 2 ? qsTr("Type at least two characters. Every word you type must appear in the package's name, description or the names it provides.") : qsTr("Check the spelling, or try fewer words. The application may be in the AUR, on Flathub or offered by its maker in another format: the Install page looks there.")) : ""
        }

        delegate: QQC2.ItemDelegate {
            width: ListView.view.width
            onClicked: page.open(modelData)
            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing
                QQC2.CheckBox {
                    visible: page.selecting
                    checked: !!page.picked[modelData.name]
                    onToggled: page.pick(modelData.name, checked)
                }
                Kirigami.Icon {
                    source: "package-x-generic"
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        font.bold: true
                        text: modelData.name + "  " + modelData.version + (modelData.repo ? "  ·  " + modelData.repo : "")
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        opacity: 0.7
                        text: modelData.desc
                    }
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: text !== ""
                    opacity: 0.8
                    text: page.statusOf(modelData) + (modelData.unneeded ? (page.statusOf(modelData) ? " · " : "") + "No longer needed" : "")
                }
            }
        }
    }

    actions: [
        Kirigami.Action {
            text: qsTr("Clean cache…")
            icon.name: "edit-clear-all"
            enabled: page.busyText === ""
            onTriggered: page.openCache()
        },
        Kirigami.Action {
            text: qsTr("Reload")
            icon.name: "view-refresh"
            enabled: !page.loadingInstalled
            onTriggered: {
                page.loadInstalled();
                page.search();
            }
        }
    ]

    Component.onCompleted: loadInstalled()

    Kirigami.Dialog {
        id: cacheDialog
        title: qsTr("Clean the package cache")
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 34
        customFooterActions: [
            Kirigami.Action {
                objectName: "cacheAction"
                text: qsTr("Clean…")
                icon.name: "edit-clear-all"
                enabled: !!(page.cacheShown && page.cacheShown.files > 0) && page.busyText === ""
                onTriggered: {
                    cacheDialog.close();
                    page.planCache();
                }
            }
        ]
        ColumnLayout {
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: qsTr("pacman keeps every package it downloaded. This deletes old ones. The installed version of every package always stays, so nothing here can break what is installed; a deleted file that is no longer on the mirrors cannot be fetched again, and the version is then gone for going back to it.")
            }
            RowLayout {
                QQC2.Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: qsTr("Older versions to keep besides the installed one:")
                }
                QQC2.SpinBox {
                    id: cacheKeep
                    objectName: "cacheKeep"
                    from: 0
                    to: 10
                    value: 2
                    onValueChanged: if (cacheDialog.opened)
                        page.previewCache()
                }
            }
            QQC2.CheckBox {
                id: cacheUninstalled
                objectName: "cacheUninstalled"
                text: qsTr("Packages that are not installed lose every cached version")
                onToggled: page.previewCache()
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font.bold: true
                text: !page.cacheShown ? qsTr("Looking…") : page.cacheShown.files === 0 ? qsTr("Nothing would be deleted with these settings.") : page.cacheShown.files + " files (" + page.sizeText(page.cacheShown.bytes) + ") of " + page.cacheShown.packages + " package versions would be deleted."
            }
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                visible: !!(page.cacheShown && page.cacheShown.problems.length)
                text: !page.cacheShown ? "" : "Left alone: " + page.cacheShown.problems.join(" ")
            }
        }
    }

    Kirigami.Dialog {
        id: confirmDialog
        title: page.confirmPlan ? root.flat(page.confirmPlan.dialog_title) : ""
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 34
        onClosed: if (page.busyText === "")
            page.confirmPlan = null
        customFooterActions: [
            Kirigami.Action {
                objectName: "listConfirmAction"
                text: page.confirmPlan ? page.confirmPlan.action : "Continue"
                icon.name: page.confirmPlan && page.confirmPlan.cache ? "edit-clear-all" : "edit-delete"
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
                text: page.confirmPlan ? root.flat(page.confirmPlan.message) : ""
            }
            QQC2.TextArea {
                textFormat: TextEdit.PlainText
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 10
                readOnly: true
                font.family: "monospace"
                visible: !!(page.confirmPlan && ((page.confirmPlan.removals || []).length || (page.confirmPlan.cache && page.confirmPlan.cache.examples.length)))
                text: !page.confirmPlan ? "" : page.confirmPlan.cache ? page.confirmPlan.cache.examples.join("\n") + (page.confirmPlan.cache.packages > page.confirmPlan.cache.examples.length ? "\n… and " + (page.confirmPlan.cache.packages - page.confirmPlan.cache.examples.length) + " more" : "") : (page.confirmPlan.removals || []).map(u => u.name + (u.reason ? "  (" + u.reason + ")" : "")).join("\n")
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
        title: qsTr("Remove packages Cygnus did not install?")
        standardButtons: Kirigami.Dialog.Cancel
        preferredWidth: Kirigami.Units.gridUnit * 30
        onClosed: page.warnTicked = false
        customFooterActions: [
            Kirigami.Action {
                objectName: "listWarnAction"
                text: qsTr("Go on…")
                icon.name: "edit-delete"
                enabled: page.warnTicked
                onTriggered: {
                    warnDialog.close();
                    page.planRemoveSelected(true);
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
            QQC2.Label {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                opacity: 0.8
                text: page.warnPlan ? "Packages: " + page.warnPlan.names.join(", ") : ""
            }
            QQC2.CheckBox {
                text: qsTr("I know what these packages are and I want them removed")
                checked: page.warnTicked
                onToggled: page.warnTicked = checked
            }
        }
    }
}
