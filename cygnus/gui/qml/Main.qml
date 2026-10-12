import QtQuick
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root
    title: qsTr("Cygnus")
    width: Kirigami.Units.gridUnit * 62
    height: Kirigami.Units.gridUnit * 42
    minimumWidth: Kirigami.Units.gridUnit * 26

    // request id -> callback; the backend answers every request through `finished`
    property var pending: ({})
    property var progressHandlers: ({})
    property var fractionHandlers: ({}) // request id -> (how far 0..1, what it is doing): only where a real count exists

    // Text from packages, manifests, repositories or commands is never interpreted as markup.
    // qsTr("%1 of %2").arg(a).arg(b) replaces the markers one after the other, so a value that itself contains "%2" would be replaced again.
    // This fills every marker in one pass, and is what a text with more than one value uses.
    function fill(template, values) {
        return String(template).replace(/%(\d+)/g, (marker, n) => n >= 1 && n <= values.length ? String(values[n - 1]) : marker);
    }

    readonly property string appId: "io.github.omranabdulaziz.Cygnus" // an identifier, not text for a person: it is never translated

    function plain(text) {
        return String(text === undefined || text === null ? "" : text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    // For places that cannot be switched to plain text (dialog titles, card banners).
    function flat(text) {
        return String(text === undefined || text === null ? "" : text).replace(/[<>]/g, "");
    }

    function request(rid, callback, onProgress, onFraction) {
        pending[rid] = callback;
        if (onProgress)
            progressHandlers[rid] = onProgress;
        if (onFraction)
            fractionHandlers[rid] = onFraction;
    }

    // Is the page in front in the middle of something (an install, a check, a question waiting for your answer)? A link
    // handed to Cygnus must not replace it then.
    function pageBusy() {
        return isBusy(pageStack.currentItem);
    }

    // `page` is typed loosely on purpose: each kind of page has its own state properties, and a page that lacks one is not busy with it.
    function isBusy(page: var): bool {
        if (!page)
            return false;
        return !!(page.installing || page.analysing || page.checking || (page.busyText !== undefined && page.busyText !== "") || (page.confirmPlan !== undefined && page.confirmPlan !== null));
    }

    function notify(text) {
        showPassiveNotification(String(text));
    }

    // Why an answer was not a success, in words: what the backend said, and never an empty message.
    function why(r) {
        const text = r.ok ? (r.result && (r.result.error || r.result.detail || r.result.state)) : r.error;
        return text ? String(text) : qsTr("it did not finish");
    }

    // An answer for a page the person has already left: the work itself carried on (the backend records what it did), but
    // the page that would have shown a failure, or a note that something needs a look, is gone, so say it here instead.
    function lateAnswer(what, r) {
        const result = r.ok ? r.result : null;
        const failed = !r.ok || (result && result.ok === false) ? why(r) : "";
        const attention = result && result.attention ? result.attention : "";
        const lateOnly = result && result.late_attention ? result.late_attention : ""; // for a page that is gone: the page itself goes on with it
        if (failed)
            notify(flat(what) + " did not finish: " + flat(failed));
        else if (attention || lateOnly)
            notify(flat(what) + ": " + flat([attention, lateOnly].filter(x => x).join(" ")));
    }

    function showPage(component, properties) {
        pageStack.clear();
        pageStack.push(component, properties || {});
    }

    // Open a page by name (used for command-line arguments such as "open this file with Cygnus").
    function openPage(name, properties) {
        const pages = {
            apps: appsPage,
            app: appPage,
            install: installPage,
            discover: discoverPage,
            discoverApp: discoverAppPage,
            packages: packagesPage,
            package: packagePage,
            updates: updatesPage,
            storage: storagePage,
            settings: settingsPage
        };
        if (pages[name])
            showPage(pages[name], properties);
    }

    // Save a picture of the window (diagnostics / bug reports).
    function captureTo(path) {
        const item = pageStack;
        const ok = item.grabToImage(function (result) {
            console.log("captured", result.saveToFile(path));
        });
        if (!ok)
            console.log("capture failed: window", item.Window.window !== null, "visible", item.visible, "size", item.width, item.height, "visibility", root.visibility);
    }

    Connections {
        target: backend
        function onFinished(rid, payload) {
            const cb = root.pending[rid];
            delete root.pending[rid];
            delete root.progressHandlers[rid];
            delete root.fractionHandlers[rid];
            if (cb)
                cb(JSON.parse(payload));
        }
        function onProgress(rid, line) {
            const cb = root.progressHandlers[rid];
            if (cb)
                cb(line);
        }
        function onProgressValue(rid, value, text) {
            const cb = root.fractionHandlers[rid];
            if (cb)
                cb(value, text);
        }
    }

    globalDrawer: Kirigami.GlobalDrawer {
        title: qsTr("Cygnus")
        titleIcon: "system-software-install"
        isMenu: !root.wideScreen
        modal: !root.wideScreen
        actions: [
            Kirigami.Action {
                text: qsTr("Applications")
                icon.name: "view-list-icons"
                onTriggered: root.showPage(appsPage)
            },
            Kirigami.Action {
                text: qsTr("Install")
                icon.name: "list-add"
                onTriggered: root.showPage(installPage)
            },
            Kirigami.Action {
                text: qsTr("Discover")
                icon.name: "system-software-install"
                onTriggered: root.showPage(discoverPage)
            },
            Kirigami.Action {
                text: qsTr("Packages")
                icon.name: "package-x-generic"
                onTriggered: root.showPage(packagesPage)
            },
            Kirigami.Action {
                text: qsTr("Updates")
                icon.name: "update-none"
                onTriggered: root.showPage(updatesPage)
            },
            Kirigami.Action {
                text: qsTr("Storage")
                icon.name: "drive-harddisk"
                onTriggered: root.showPage(storagePage)
            },
            Kirigami.Action {
                text: qsTr("Settings")
                icon.name: "configure"
                onTriggered: root.showPage(settingsPage)
            }
        ]
    }

    Component {
        id: appsPage
        AppsPage {}
    }
    Component {
        id: appPage
        AppPage {}
    }
    Component {
        id: installPage
        InstallPage {}
    }
    Component {
        id: discoverPage
        DiscoverPage {}
    }
    Component {
        id: discoverAppPage
        DiscoverAppPage {}
    }
    Component {
        id: packagesPage
        PackagesPage {}
    }
    Component {
        id: packagePage
        PackagePage {}
    }
    Component {
        id: updatesPage
        UpdatesPage {}
    }
    Component {
        id: storagePage
        StoragePage {}
    }
    Component {
        id: settingsPage
        SettingsPage {}
    }

    pageStack.initialPage: AppsPage {}
}
