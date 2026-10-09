import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Dialogs
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
        if (onProgress) { // a new request starts from "not known": the last one's number must not stay on the bar
            page.progressValue = -1;
            page.progressLabel = "";
        }
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
    title: "Install"

    property string fileUrl: ""
    property string launchNotice: "" // why something handed to Cygnus (a link, a file) could not be opened, or what it asked for
    property string appSpec: "" // a Flatpak app id from a configured repository, e.g. flathub:org.kde.kcalc
    property var plan: null
    property bool analysing: false
    property bool installing: false
    property int analysisSeq: 0 // only the newest analysis may set the plan
    property string log: ""
    property var optionalChosen: ({}) // optional libraries the person ticked: {package: true}
    property bool scriptsShown: false
    property bool effectsShown: false

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

    function noteLine(line) {
        log += line + "\n";
        progressLabel = line;
    }

    function chosenOptional() {
        return Object.keys(optionalChosen).filter(k => optionalChosen[k]);
    }
    property var storage: JSON.parse(backend ? backend.storageJson : "{\"locations\": [], \"candidates\": []}")
    readonly property var locationLabels: storage.locations.map(l => l.label)

    // Text that comes from a package or repository is never interpreted as markup.
    function plain(text) {
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function humanSize(n) {
        if (!n)
            return "";
        const units = [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
        for (const [u, d] of units)
            if (n >= d)
                return (n / d).toFixed(n >= 10 * d ? 0 : 1) + " " + u;
        return n + " B";
    }

    function analyse() {
        if ((!fileUrl && !appSpec) || installing)
            return;
        analysing = true;
        plan = null;
        afterCommit = null;
        confirmPlan = null;
        log = ""; // the log belongs to the last install, not to what is shown now
        optionalChosen = ({});
        scriptsShown = false;
        effectsShown = false;
        acceptScripts.checked = false;
        progressValue = -1;
        progressLabel = "";
        appSpecField.text = appSpec;
        doneMessage.visible = false; // that banner names the previous install, not what is shown now
        const seq = ++analysisSeq;
        // Install acts on exactly what was analysed, even if the page's fields change meanwhile.
        const subject = {
            fileUrl: fileUrl,
            appSpec: appSpec,
            location: locationBox.currentText
        };
        const aur = appSpec.startsWith("aur:");
        reviewed.checked = false;
        const rid = aur ? backend.aurReview(appSpec.slice(4)) : appSpec ? backend.analyseFlatpakRef(appSpec, subject.location) : backend.analyseFile(fileUrl, subject.location);
        page.ask(rid, function (r) {
            if (seq !== analysisSeq)
                return; // a newer analysis replaced this one
            analysing = false;
            if (!r.ok) {
                page.showError(r.error);
                return;
            }
            errorMessage.visible = false;
            plan = Object.assign(aur ? Object.assign({
                kind: "aur",
                format: "aur",
                installable: r.result.buildable,
                placements: [],
                issues: [],
                verdict: r.result.description
            }, r.result) : r.result, {
                subject: subject
            });
        }, function (line) {
            page.progressLabel = line;
        });
    }

    // AUR: dependencies from your repositories (password) → build as you → install (password).
    // AUR: dependencies from your repositories (password) → for every AUR package, dependencies
    // first: build as you → install (password); each is recorded so it can be removed later.
    function installAur() {
        const review = plan;
        const queue = review.dependencies.map(d => ({
                    pkgbase: d.pkgbase,
                    commit: d.commit,
                    names: d.names,
                    version: d.version,
                    dependency: true
                })).concat([
            {
                pkgbase: review.pkgbase,
                commit: review.commit,
                names: [review.name],
                version: review.version,
                dependency: false
            }
        ]);
        const next = function () {
            const item = queue.shift();
            installing = true;
            page.ask(backend.aurBuild(item.pkgbase, item.commit, JSON.stringify(item.names)), function (b) {
                if (!b.ok) {
                    finish(b);
                    return;
                }
                // The backend registers the package once it is installed, even if you leave this page meanwhile.
                const record = item.dependency ? {
                    kind: "aur",
                    pkgbase: item.pkgbase,
                    name: item.names[0],
                    commit: item.commit,
                    version: item.version,
                    names: item.names,
                    dependency_of: review.name
                } : {
                    kind: "aur",
                    pkgbase: item.pkgbase,
                    name: item.names[0],
                    commit: item.commit,
                    version: item.version
                };
                page.ask(backend.planBuiltPackages(JSON.stringify(b.result.packages), item.dependency, JSON.stringify(record)), function (p) {
                    installing = false;
                    if (!p.ok) {
                        finish(p);
                        return;
                    }
                    afterCommit = function () {
                        if (queue.length) {
                            next();
                            return "continue";
                        }
                    };
                    confirmPlan = p.result;
                    confirmDialog.open();
                });
            }, function (line) {
                page.noteLine(line);
            });
        };
        if (!review.repo_dependencies.length) {
            next();
            return;
        }
        page.ask(backend.planRepoDependencies(JSON.stringify(review.repo_dependencies)), function (p) {
            if (!p.ok) {
                finish(p);
                return;
            }
            afterCommit = function () {
                next();
                return "continue";
            };
            confirmPlan = p.result;
            confirmDialog.open();
        });
    }

    property var unfinished: [] // AUR installs left half-way (a dependency was installed, the package itself was not)

    function refreshUnfinished() {
        page.ask(backend.interruptedAurChains(), function (r) {
            if (r.ok)
                unfinished = r.result;
        });
    }

    // Starts again from a fresh review: nothing is built or installed without it.
    function resumeChain(chain) {
        unfinished = unfinished.filter(c => c.target !== chain.target);
        appSpecField.text = "aur:" + chain.target;
        fileUrl = "";
        appSpec = appSpecField.text;
        analyse();
    }

    function dismissChain(chain) {
        unfinished = unfinished.filter(c => c.target !== chain.target);
        page.ask(backend.dismissAurChain(chain.target), function (r) {});
    }

    property var confirmPlan: null
    property var afterCommit: null // UI follow-up only, e.g. go on with the next package of a chain; records are made by the backend

    function finish(r) {
        installing = false;
        confirmPlan = null; // the question was answered: a page with an old one left over would count as busy for ever
        if (r.ok && r.result.ok) {
            const next = afterCommit;
            afterCommit = null;
            if (next && next() === "continue")
                return;
            doneMessage.visible = true;
            if (r.result.attention) { // e.g. an old folder that changed during a move was kept, not deleted
                page.showAttention(r.result.attention);
            }
        } else {
            page.showError(root.why(r));
        }
    }

    function install() {
        if (!plan || analysing)
            return;
        installing = true;
        log = "";
        afterCommit = null;
        doneMessage.visible = false;
        const s = plan.subject; // the file, Flatpak id and location the shown plan was made for
        const onLine = function (line) {
            page.noteLine(line);
        };
        if (plan.kind === "aur") {
            installAur(); // stays busy: planning the dependencies takes a moment, and the page must not change under it
        } else if (plan.format === "appimage") {
            page.ask(backend.installFile(s.fileUrl, plan.target || s.location), finish, onLine);
        } else if (plan.format === "flatpak") {
            page.ask(backend.installFlatpakRef(s.appSpec, plan.target || s.location), finish, onLine);
        } else if (plan.format === "flatpak-bundle") {
            page.ask(backend.installFlatpakBundle(s.fileUrl, plan.target || s.location), finish, onLine);
        } else if (plan.kind === "foreign") {
            page.ask(backend.convertForeign(s.fileUrl, plan.sha256 || "", JSON.stringify(page.chosenOptional()), acceptScripts.checked), function (r) {
                if (!r.ok) {
                    installing = false;
                    page.showError(r.error);
                    return;
                }
                const converted = r.result;
                page.log += "Converted: " + converted.notes.join("; ") + "\n";
                const record = {
                    kind: "converted",
                    name: converted.name,
                    version: converted.version,
                    source: converted.source,
                    vendor_package: converted.vendor_package,
                    vendor_format: converted.vendor_format
                };
                page.ask(backend.planBuiltPackage(converted.package, JSON.stringify(record)), function (p) {
                    installing = false;
                    if (!p.ok) {
                        page.showError(p.error);
                        return;
                    }
                    confirmPlan = p.result;
                    confirmDialog.open();
                });
            }, onLine);
        } else if (plan.format === "localpkg") {
            page.ask(backend.planLocalPackage(s.fileUrl), function (r) {
                installing = false;
                if (!r.ok) {
                    page.showError(r.error);
                    return;
                }
                confirmPlan = r.result;
                confirmDialog.open();
            });
        }
    }

    Kirigami.PromptDialog {
        id: confirmDialog
        property bool confirmed: false
        title: "Confirm installation"
        subtitle: page.confirmPlan ? root.flat(page.confirmPlan.message + (page.confirmPlan.security_note ? "\n\n" + page.confirmPlan.security_note : "") + (page.confirmPlan.relogin ? "\n\nYou will need to log out and back in afterwards." : "")) : ""
        standardButtons: Kirigami.Dialog.Cancel
        onOpened: confirmed = false
        onClosed: {
            if (!confirmed) { // cancelled in any way: nothing queued for "afterwards" may run later
                page.afterCommit = null;
                page.confirmPlan = null;
                page.installing = false;
                if (page.plan && page.plan.kind === "aur")
                    page.refreshUnfinished(); // a dependency may already be installed: say that this one was not finished
            }
        }
        customFooterActions: [
            Kirigami.Action {
                text: "Install"
                icon.name: "dialog-ok"
                onTriggered: {
                    if (confirmDialog.confirmed) // a second click while the dialog closes
                        return;
                    confirmDialog.confirmed = true;
                    confirmDialog.close();
                    page.installing = true;
                    page.ask(backend.commitFix(page.confirmPlan.token), page.finish, function (line) {
                        page.noteLine(line);
                    });
                }
            }
        ]
    }

    FileDialog {
        id: fileDialog
        title: "Choose an application to install"
        nameFilters: ["Applications (*.AppImage *.appimage *.flatpak *.flatpakref *.pkg.tar.zst *.pkg.tar.xz *.deb *.rpm)", "All files (*)"]
        onAccepted: {
            if (page.installing)
                return;
            page.appSpec = "";
            page.fileUrl = selectedFile.toString();
            page.analyse();
        }
    }

    DropArea {
        anchors.fill: parent
        onDropped: drop => {
            if (drop.hasUrls && !page.installing) {
                page.appSpec = "";
                page.fileUrl = drop.urls[0].toString();
                page.analyse();
            }
        }
    }

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
            id: launchMessage
            Layout.fillWidth: true
            type: Kirigami.MessageType.Information
            text: root.plain(page.launchNotice)
            showCloseButton: true
            visible: page.launchNotice !== ""
        }
        Kirigami.InlineMessage {
            id: unfinishedMessage
            Layout.fillWidth: true
            type: Kirigami.MessageType.Warning
            visible: page.unfinished.length > 0 && !page.installing
            text: page.unfinished.length ? root.plain(page.unfinished[0].target) + " was not finished: " + root.plain(page.unfinished[0].dependencies.join(", ")) + (page.unfinished[0].dependencies.length > 1 ? " were installed as its dependencies, but " : " was installed as its dependency, but ") + root.plain(page.unfinished[0].target) + " itself was not built and installed." : ""
            actions: [
                Kirigami.Action {
                    text: "Continue"
                    icon.name: "go-next"
                    onTriggered: page.resumeChain(page.unfinished[0])
                },
                Kirigami.Action {
                    text: "Dismiss"
                    onTriggered: page.dismissChain(page.unfinished[0])
                }
            ]
        }
        Kirigami.InlineMessage {
            id: doneMessage
            Layout.fillWidth: true
            type: Kirigami.MessageType.Positive
            text: root.plain(page.plan ? page.plan.name : "The application") + " is installed." + (page.plan && (page.plan.format === "appimage" || page.plan.format === "flatpak" || page.plan.format === "flatpak-bundle") ? " You can find it in your application menu." : "")
            showCloseButton: true
            visible: false
        }

        RowLayout {
            QQC2.Button {
                text: page.fileUrl ? "Choose another file…" : "Choose a file…"
                icon.name: "document-open"
                onClicked: fileDialog.open()
            }
            QQC2.Label {
                textFormat: Text.PlainText
                text: "Store on:"
            }
            QQC2.ComboBox {
                id: locationBox
                model: page.locationLabels.length ? page.locationLabels : ["(no storage location yet)"]
                onActivated: page.analyse()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            QQC2.TextField {
                id: appSpecField
                Layout.fillWidth: true
                placeholderText: "Or a Flathub app id (org.kde.kcalc), another repository (remote:app.id), or aur:name"
                onAccepted: {
                    if (lookUp.enabled) // Enter must not do what the greyed-out button would not
                        lookUp.clicked();
                }
            }
            QQC2.Button {
                id: lookUp
                text: "Look up"
                icon.name: "search"
                enabled: appSpecField.text.trim() !== "" && !page.installing
                onClicked: {
                    page.fileUrl = "";
                    page.appSpec = appSpecField.text.trim();
                    page.analyse();
                }
            }
        }
        Kirigami.PlaceholderMessage {
            Layout.fillWidth: true
            visible: !page.fileUrl && !page.appSpec
            icon.name: "document-import"
            text: "Drop an application here"
            explanation: "AppImage, Flatpak bundle, Arch package, .deb or .rpm"
        }
        ProgressStrip {
            Layout.fillWidth: true
            running: page.analysing || page.installing
            value: page.progressValue
            text: page.progressLabel !== "" ? page.progressLabel : (page.analysing ? "Looking at the file…" : "Working…")
        }

        ColumnLayout {
            Layout.fillWidth: true
            visible: !!(page.plan && page.plan.kind === "aur")
            spacing: Kirigami.Units.smallSpacing
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: true
                type: Kirigami.MessageType.Warning
                text: "AUR packages are build scripts written by community members. Building runs this script as you; installing gives the result administrator rights. Read it before you continue."
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                text: page.plan && page.plan.kind === "aur" ? ("Maintainer " + page.plan.maintainer + " · " + page.plan.votes + " votes" + (page.plan.out_of_date ? " · flagged out of date" : "") + (page.plan.repo_dependencies.length ? " · needs " + page.plan.repo_dependencies.join(", ") + " from your repositories" : "") + (page.plan.dependencies.length ? " · needs " + page.plan.dependencies.length + " other AUR package" + (page.plan.dependencies.length > 1 ? "s" : "") + ", reviewed below" : "")) : ""
            }
            Repeater {
                model: page.plan && page.plan.kind === "aur" ? page.plan.blockers : []
                delegate: Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: true
                    type: Kirigami.MessageType.Error
                    text: page.plain(modelData)
                }
            }
            Repeater {
                model: page.plan && page.plan.kind === "aur" ? page.plan.dependencies.concat([page.plan]) : []
                delegate: ColumnLayout {
                    id: pkgReview
                    Layout.fillWidth: true
                    required property var modelData
                    Kirigami.Heading {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        level: 3
                        text: pkgReview.modelData.pkgbase + (pkgReview.modelData === page.plan ? "" : "  (dependency)") + (pkgReview.modelData.previously_approved === pkgReview.modelData.commit ? " · you approved this exact version before" : pkgReview.modelData.previously_approved ? " · showing what changed since the version you approved" : "")
                    }
                    Repeater {
                        model: pkgReview.modelData.problems
                        delegate: Kirigami.InlineMessage {
                            Layout.fillWidth: true
                            visible: true
                            type: Kirigami.MessageType.Error
                            text: "Cannot be reviewed in full, so Cygnus will not build it: " + page.plain(modelData)
                        }
                    }
                    Repeater {
                        model: pkgReview.modelData.hints
                        delegate: QQC2.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: Kirigami.Theme.negativeTextColor
                            textFormat: Text.PlainText
                            text: "⚠ " + modelData.file + ":" + modelData.line + " " + modelData.why + ": " + modelData.text
                        }
                    }
                    Repeater {
                        model: pkgReview.modelData.diff ? [["Changes since the version you approved", pkgReview.modelData.diff]] : Object.entries(pkgReview.modelData.files)
                        delegate: ColumnLayout {
                            Layout.fillWidth: true
                            Kirigami.Heading {
                                textFormat: Text.PlainText
                                level: 4
                                text: modelData[0]
                            }
                            QQC2.TextArea {
                                Layout.fillWidth: true
                                readOnly: true
                                wrapMode: Text.NoWrap
                                textFormat: TextEdit.PlainText
                                font.family: "monospace"
                                text: modelData[1]
                            }
                        }
                    }
                }
            }
            QQC2.CheckBox {
                id: reviewed
                text: page.plan && page.plan.kind === "aur" && page.plan.dependencies.length ? "I have read the build files of all " + (page.plan.dependencies.length + 1) + " packages" : "I have read the build files"
            }
        }

        Kirigami.Card {
            Layout.fillWidth: true
            visible: !!page.plan
            banner.title: page.plan ? root.flat("Install " + page.plan.name + (page.plan.version ? " " + page.plan.version : "")) : ""
            banner.titleIcon: "system-software-install"
            contentItem: ColumnLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing
                QQC2.Label {
                    visible: !!(page.plan && page.plan.verdict)
                    textFormat: Text.PlainText
                    text: page.plan && page.plan.verdict ? page.plan.verdict : ""
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Repeater {
                    model: page.plan ? page.plan.placements : []
                    delegate: RowLayout {
                        Layout.fillWidth: true
                        QQC2.Label {
                            textFormat: Text.PlainText
                            text: modelData.name + ":"
                            font.bold: true
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            text: modelData.location + (modelData.bytes ? "   " + page.humanSize(modelData.bytes) : "")
                        }
                        QQC2.Label {
                            textFormat: Text.PlainText
                            text: modelData.reason
                            opacity: 0.6
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                    }
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: !!(page.plan && page.plan.runtime)
                    text: "Runtime: " + (page.plan ? page.plan.runtime : "")
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: !!(page.plan && page.plan.components && page.plan.components.length)
                    text: page.plan && page.plan.components ? "Components: " + page.plan.components.map(c => c.name + (c.relation === "optional" ? " (optional)" : "") + (c.already_present ? " ✓" : "")).join(" · ") : ""
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Repeater {
                    model: page.plan && page.plan.usage ? Object.keys(page.plan.usage) : []
                    delegate: QQC2.Label {
                        textFormat: Text.PlainText
                        text: "Estimated " + modelData + " usage: " + page.humanSize(page.plan.usage[modelData])
                    }
                }
                Repeater {
                    model: page.plan ? page.plan.notes || [] : []
                    delegate: QQC2.Label {
                        textFormat: Text.PlainText
                        text: modelData
                        opacity: 0.7
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
                Repeater {
                    model: page.plan ? page.plan.issues : []
                    delegate: Kirigami.InlineMessage {
                        Layout.fillWidth: true
                        visible: true
                        type: modelData.severity === "blocker" ? Kirigami.MessageType.Error : modelData.severity === "degraded" ? Kirigami.MessageType.Warning : Kirigami.MessageType.Information
                        text: "<b>" + root.plain(modelData.title) + "</b><br>" + root.plain(modelData.explanation) + (modelData.resolutions.length ? "<br><i>Suggested: " + root.plain(modelData.resolutions[0].title) + "</i>" : "")
                    }
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: !!(page.plan && page.plan.kind === "foreign" && page.plan.depends && page.plan.depends.length)
                    text: page.plan && page.plan.depends ? "Also installed from your repositories, because it needs them: " + page.plan.depends.join(", ") : ""
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                Repeater {
                    model: page.plan && page.plan.kind === "foreign" && page.plan.additions ? page.plan.additions : []
                    delegate: QQC2.Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: "Cygnus also adds " + modelData.text + ", which the vendor's install script would have set up."
                        opacity: 0.8
                    }
                }
                Repeater {
                    model: page.plan && page.plan.kind === "foreign" && page.plan.optional ? page.plan.optional : []
                    delegate: RowLayout {
                        id: optionalRow
                        required property var modelData
                        Layout.fillWidth: true
                        QQC2.CheckBox {
                            id: optionalBox
                            Layout.fillWidth: true
                            checked: page.optionalChosen[optionalRow.modelData.package] === true
                            // names come from the package: no angle brackets, so the text can never be read as markup
                            text: root.flat("Also install " + optionalRow.modelData.package + " (optional): used by " + optionalRow.modelData.files.join(", ") + ". Without it the program works, but that part does not.")
                            onToggled: {
                                const chosen = Object.assign({}, page.optionalChosen);
                                chosen[optionalRow.modelData.package] = checked;
                                page.optionalChosen = chosen;
                            }
                            contentItem: QQC2.Label {
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                leftPadding: optionalBox.indicator ? optionalBox.indicator.width + optionalBox.spacing : 0
                                text: optionalBox.text
                            }
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: !!(page.plan && page.plan.installable_after_review && page.plan.scripts)
                    spacing: Kirigami.Units.smallSpacing
                    Kirigami.InlineMessage {
                        Layout.fillWidth: true
                        visible: true
                        type: Kirigami.MessageType.Warning
                        text: "Cygnus could not fully read this package's install scripts. Converting never runs them, so nothing they would do (adding a software repository, creating groups, changing settings) happens. If the program needs one of those things it may not work properly, and a program that updates itself through the vendor's repository will not do that: update it by converting the newer file."
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        opacity: 0.7
                        text: page.plan && page.plan.scripts ? "Why they could not be read: " + page.plan.scripts.unreadable.join("; ") : ""
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        visible: !!(page.plan && page.plan.scripts && page.plan.scripts.mentions.length)
                        text: "The scripts mention:"
                        font.bold: true
                    }
                    Repeater {
                        model: page.plan && page.plan.scripts ? page.plan.scripts.mentions : []
                        delegate: QQC2.Label {
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                            font.bold: modelData.important
                            text: (modelData.important ? "! " : "• ") + modelData.command + ": " + modelData.what + " (" + modelData.scripts.join(", ") + ")" + (modelData.important ? "; if the program needs this, it will be missing" : "")
                        }
                    }
                    QQC2.Button {
                        visible: !!(page.plan && page.plan.scripts && page.plan.scripts.effects && page.plan.scripts.effects.length)
                        text: page.effectsShown ? "Hide what the scripts would have done" : "Show what the scripts would have done to your system"
                        onClicked: page.effectsShown = !page.effectsShown
                    }
                    QQC2.Label {
                        textFormat: Text.PlainText
                        visible: page.effectsShown
                        text: "Read from the scripts' text; nothing was run. Steps marked \"only if a condition holds\" may not happen at all."
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        opacity: 0.7
                    }
                    Repeater {
                        model: page.effectsShown && page.plan && page.plan.scripts && page.plan.scripts.effects ? page.plan.scripts.effects : []
                        delegate: ColumnLayout {
                            id: effectGroup
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 0
                            QQC2.Label {
                                textFormat: Text.PlainText
                                text: effectGroup.modelData.title
                                opacity: 0.85
                            }
                            Repeater {
                                model: effectGroup.modelData.items
                                delegate: QQC2.Label {
                                    textFormat: Text.PlainText
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                    Layout.leftMargin: Kirigami.Units.largeSpacing
                                    font.family: "monospace"
                                    text: "• " + modelData
                                }
                            }
                        }
                    }
                    QQC2.Button {
                        text: page.scriptsShown ? "Hide the scripts" : "Show the scripts"
                        onClicked: page.scriptsShown = !page.scriptsShown
                    }
                    Repeater {
                        model: page.scriptsShown && page.plan && page.plan.scripts ? Object.keys(page.plan.scripts.texts) : []
                        delegate: ColumnLayout {
                            id: scriptView
                            required property string modelData
                            Layout.fillWidth: true
                            Kirigami.Heading {
                                textFormat: Text.PlainText
                                level: 5
                                text: scriptView.modelData
                            }
                            QQC2.ScrollView {
                                Layout.fillWidth: true
                                Layout.preferredHeight: Kirigami.Units.gridUnit * 14
                                QQC2.TextArea {
                                    textFormat: TextEdit.PlainText
                                    readOnly: true
                                    font.family: "monospace"
                                    wrapMode: TextEdit.NoWrap
                                    text: page.plan.scripts.texts[scriptView.modelData]
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        QQC2.CheckBox {
                            id: acceptScripts
                            Layout.fillWidth: true
                            text: "I have read what the scripts do, and I understand that they will not be run."
                            contentItem: QQC2.Label {
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                leftPadding: acceptScripts.indicator ? acceptScripts.indicator.width + acceptScripts.spacing : 0
                                text: acceptScripts.text
                            }
                        }
                    }
                }
                QQC2.Label {
                    textFormat: Text.PlainText
                    visible: page.log !== ""
                    text: page.log
                    font.family: "monospace"
                    opacity: 0.8
                }
                RowLayout {
                    Layout.alignment: Qt.AlignRight
                    QQC2.Button {
                        text: page.plan && page.plan.kind === "foreign" ? "Convert and install" : page.plan && page.plan.kind === "aur" ? "Build and install" : "Install"
                        icon.name: "download"
                        enabled: !!(page.plan && (page.plan.installable || (page.plan.installable_after_review && acceptScripts.checked))) && !page.installing && (page.plan.kind !== "aur" || reviewed.checked)
                        onClicked: page.install()
                    }
                }
            }
        }
    }

    Connections {
        target: backend
        function onStorageChanged() {
            // "Open with" analyses before the storage list has arrived; once it is known, look again.
            const stalled = page.plan && page.plan.issues && page.plan.issues.some(i => i.code === "STORAGE_NOT_SET_UP");
            if (stalled && page.locationLabels.length && !page.installing && !page.analysing)
                Qt.callLater(page.analyse);
        }
    }

    Component.onCompleted: {
        backend.refreshStorage();
        appSpecField.text = appSpec;
        refreshUnfinished();
        if (fileUrl || appSpec)
            analyse();
    }
}
