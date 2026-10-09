import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.kirigamiaddons.formcard as FormCard

FormCard.FormCardPage {
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
    title: "Settings"

    property bool changing: false // a switch is being applied: the others wait, so two answers cannot cross
    property bool watchAvailable: false
    property bool handlerAvailable: false
    property var parts: [] // Cygnus's optional tools: [{id, package, what, present}]
    property var partsPlan: null // the helper's plan for installing the missing ones, while the person decides
    property string partsMessage: ""

    function loadParts() {
        page.ask(backend.optionalParts(), function (r) {
            if (r.ok)
                page.parts = r.result;
        });
    }

    function installParts(packages) {
        page.changing = true;
        partsMessage = "";
        page.ask(backend.planOptionalParts(JSON.stringify(packages)), function (r) {
            page.changing = false;
            if (!r.ok) {
                partsMessage = root.flat(r.error);
                return;
            }
            partsPlan = r.result;
            partsDialog.open();
        });
    }

    FormCard.FormHeader {
        title: "Safety"
    }
    FormCard.FormCard {
        FormCard.FormTextDelegate {
            text: "Community (AUR) build scripts"
            description: "Always shown for review before building. This cannot be turned off from here."
        }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate {
            text: "Administrator actions"
            description: "Every system change shows exactly what will happen in the authentication dialog."
        }
    }

    FormCard.FormHeader {
        title: "Background checks"
    }
    FormCard.FormCard {
        FormCard.FormSwitchDelegate {
            id: watchSwitch
            text: "Check for updates and problems"
            description: "Every few hours, Cygnus checks your applications and system packages and sends a notification about anything new. It never installs anything by itself."
            enabled: page.watchAvailable && !page.changing
            onToggled: {
                const wanted = checked;
                page.changing = true;
                page.ask(backend.setBackgroundChecks(wanted), function (r) {
                    page.changing = false;
                    if (r.ok) {
                        watchSwitch.checked = r.result.enabled;
                        watchNote.description = "";
                    } else {
                        watchSwitch.checked = !wanted; // back to what it was before this click
                        watchNote.description = root.flat(r.error);
                    }
                });
            }
        }
        FormCard.FormTextDelegate {
            id: watchNote
            visible: description !== ""
            text: "Note"
        }
    }

    FormCard.FormHeader {
        title: "Opening software"
    }
    FormCard.FormCard {
        FormCard.FormSwitchDelegate {
            id: handlerSwitch
            text: "Open Flathub links and software files with Cygnus"
            description: "Makes Cygnus the program that opens Flathub's Install button, .flatpakref and .flatpak files, .deb, .rpm and AppImage files. It never installs anything by itself: it shows what the file is and asks first. Turning this off puts back the program that opened them before."
            enabled: page.handlerAvailable && !page.changing
            onToggled: {
                const wanted = checked;
                page.changing = true;
                page.ask(backend.setDefaultHandler(wanted), function (r) {
                    page.changing = false;
                    if (r.ok) {
                        handlerSwitch.checked = r.result.enabled;
                        handlerNote.description = "";
                    } else {
                        handlerSwitch.checked = !wanted;
                        handlerNote.description = root.flat(r.error);
                        page.changing = true; // some types may have been switched before it failed: show what is really so
                        page.ask(backend.defaultHandler(), function (state) {
                            page.changing = false;
                            if (state.ok)
                                handlerSwitch.checked = state.result.enabled;
                        });
                    }
                });
            }
        }
        FormCard.FormTextDelegate {
            id: handlerNote
            visible: description !== ""
            text: "Note"
        }
    }

    FormCard.FormHeader {
        title: "Optional parts"
    }
    FormCard.FormCard {
        FormCard.FormTextDelegate {
            visible: page.parts.length > 0 && page.parts.every(p => p.present)
            text: "Everything optional is installed"
            description: "Cygnus can use every extra tool it knows about."
        }
        Repeater {
            model: page.parts.filter(p => !p.present)
            delegate: FormCard.FormButtonDelegate {
                required property var modelData
                text: modelData.package + " is not installed"
                description: root.flat(modelData.what)
                icon.name: "download"
                enabled: !page.changing
                onClicked: page.installParts([modelData.package])
            }
        }
        FormCard.FormTextDelegate {
            visible: page.partsMessage !== ""
            text: "Note"
            description: page.partsMessage
        }
    }

    Kirigami.PromptDialog {
        id: partsDialog
        title: page.partsPlan ? root.flat(page.partsPlan.title) : ""
        subtitle: page.partsPlan ? root.flat(page.partsPlan.message) : ""
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Install"
                icon.name: "dialog-ok"
                onTriggered: {
                    partsDialog.close();
                    if (!page.partsPlan || page.changing)
                        return;
                    const plan = page.partsPlan;
                    page.partsPlan = null;
                    page.changing = true;
                    page.partsMessage = "Installing… (you will be asked for your password)";
                    page.ask(backend.commitFix(plan.token), function (r) {
                        page.changing = false;
                        page.partsMessage = r.ok && r.result.ok ? "" : root.flat(root.why(r));
                        page.loadParts();
                    });
                }
            }
        ]
    }

    FormCard.FormHeader {
        title: "Discovery"
    }
    FormCard.FormCard {
        FormCard.FormTextDelegate {
            text: "AI-assisted discovery"
            description: "Not built yet, so there is nothing to turn on. Cygnus works fully without it. If it is added, it will only suggest things for you to review, and it will never run anything."
        }
    }

    FormCard.FormHeader {
        title: "About"
    }
    FormCard.FormCard {
        FormCard.FormTextDelegate {
            text: "Cygnus"
            description: "Application installer and manager for CachyOS"
        }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate {
            text: "Version"
            description: (Qt.application.version ? Qt.application.version + " " : "") + "(public beta). To report a problem, run `cygnus report` in a terminal and paste what it prints."
        }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate {
            text: "Application ID"
            description: "io.github.omranabdulaziz.Cygnus"
        }
    }

    Component.onCompleted: {
        page.loadParts();
        page.ask(backend.backgroundChecks(), function (r) {
            if (r.ok) {
                page.watchAvailable = r.result.available;
                watchSwitch.checked = r.result.enabled;
                watchNote.description = r.result.available ? "" : root.flat(r.result.why || "");
            } else {
                watchNote.description = root.flat(r.error);
            }
        });
        page.ask(backend.defaultHandler(), function (r) {
            if (r.ok) {
                page.handlerAvailable = r.result.available;
                handlerSwitch.checked = r.result.enabled;
                handlerNote.description = r.result.available ? "" : root.flat(r.result.why);
            } else {
                handlerNote.description = root.flat(r.error);
            }
        });
    }
}
