import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
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

    // The AI assistant. What is shown is always what is SAVED: choosing another assistant, or typing in the dialog, changes nothing until
    // it is saved there, and a cancelled dialog leaves nothing behind (a typed key is never kept).
    property bool aiBusy: false
    property var aiState: null // what the backend says: {enabled, provider, model, base_url, has_key, wallet_ok, hosted_available, ...}
    property string aiMessage: ""
    property string aiPending: "" // the assistant the dialog is being used to set up, while it is not the saved one yet
    property string aiDialogError: ""
    readonly property var aiProviderIds: ["hosted", "gemini", "compatible"]
    readonly property var aiProviderNames: ["Cygnus hosted assistant (nothing to set up)", "Google Gemini, with your own key", "OpenAI-compatible server, or one on this computer"]
    readonly property string aiProviderId: aiState ? aiState.provider : "hosted"
    readonly property string aiDialogProvider: aiPending !== "" ? aiPending : aiProviderId
    readonly property bool aiHasKey: !!aiState && aiState.has_key
    readonly property bool aiWalletOk: !!aiState && aiState.wallet_ok
    readonly property string aiProviderNote: {
        if (aiProviderId === "hosted")
            return aiState && !aiState.hosted_available ? "The hosted assistant is not available in this version yet: pick another one, or leave this off." : "Free while in beta, and limited per day for everyone. The request goes to Cygnus's own small service, hosted on Cloudflare, which asks Google's Gemini on your behalf. It needs no account and stores nothing about you, but like any website it sees your internet address, and Google may keep the public text under the terms of its free service.";
        if (aiProviderId === "gemini")
            return "Your key goes straight to Google (generativelanguage.googleapis.com) and is kept in KWallet.";
        return "For a program such as Ollama on this computer, or a server you trust. A key is optional, and is kept for this server's address only.";
    }

    function syncProviderChoice() {
        const i = aiProviderIds.indexOf(aiProviderId);
        aiProvider.currentIndex = i < 0 ? 0 : i;
    }

    function showAi(state) {
        aiState = state;
        aiSwitch.checked = state.enabled;
        syncProviderChoice();
        aiMessage = state.wallet_ok || state.provider === "hosted" ? "" : "KWallet is not available: " + root.flat(state.wallet_why || "") + " Keys cannot be saved without it.";
    }

    function loadAi() {
        page.ask(backend.aiStatus(), function (r) {
            if (r.ok)
                page.showAi(r.result);
            else
                page.aiMessage = root.flat(r.error);
        });
    }

    // The dialog for the model, the address and the key of `provider` (the saved one, or one the person is switching to).
    function openAiDialog(provider) {
        const saved = provider === aiProviderId;
        aiPending = saved ? "" : provider;
        aiDialogError = "";
        aiModel.text = saved ? aiState.model : (aiState.default_models[provider] || "");
        aiBase.text = saved ? (aiState.base_url || "") : "";
        aiKey.text = "";
        aiDialog.open();
    }

    function pickProvider(index) {
        if (aiBusy || !aiState)
            return;
        const id = aiProviderIds[index];
        if (id === aiProviderId)
            return;
        if (id === "hosted") {
            changeAi(aiState.enabled, "hosted", "", "");
        } else {
            openAiDialog(id); // it needs a key or an address: nothing is switched until the person saves
            syncProviderChoice();
        }
    }

    // Saves the settings with one change (the switch, or the shared assistant); the key is never involved here.
    function changeAi(enabled, provider, model, base) {
        if (aiBusy)
            return;
        aiBusy = true;
        aiMessage = "";
        page.ask(backend.aiConfigure(enabled, provider, model, base), function (r) {
            aiBusy = false;
            if (r.ok) {
                page.showAi(r.result);
            } else {
                aiMessage = root.flat(r.error);
                page.loadAi(); // show what is really saved
            }
        });
    }

    // The dialog's Save: the settings first, then the key if one was typed. The dialog stays open (with what was typed) when
    // something goes wrong, and says what.
    function saveAiDialog() {
        if (aiBusy)
            return;
        aiBusy = true;
        aiDialogError = "";
        const provider = aiDialogProvider;
        const typedKey = aiKey.text;
        page.ask(backend.aiConfigure(aiState.enabled, provider, aiModel.text, provider === "compatible" ? aiBase.text : ""), function (r) {
            if (!r.ok) {
                aiBusy = false;
                aiDialogError = String(r.error);
                return;
            }
            aiPending = ""; // saved: it is the saved assistant now
            if (typedKey === "") {
                aiBusy = false;
                page.showAi(r.result);
                aiDialog.close();
                return;
            }
            page.ask(backend.aiSetKey(provider, typedKey), function (k) {
                aiBusy = false;
                if (k.ok) {
                    page.showAi(k.result);
                    aiDialog.close();
                } else {
                    page.showAi(r.result);
                    aiDialogError = String(k.error) + " The other settings were saved; the key was not.";
                }
            });
        });
    }

    function removeAiKey() {
        if (aiBusy)
            return;
        aiBusy = true;
        page.ask(backend.aiClearKey(aiProviderId), function (r) {
            aiBusy = false;
            if (r.ok)
                page.showAi(r.result);
            else
                aiMessage = root.flat(r.error);
        });
    }

    function testAi() {
        if (aiBusy)
            return;
        aiBusy = true;
        aiMessage = "Testing…";
        page.ask(backend.aiTest(), function (r) {
            aiBusy = false;
            aiMessage = r.ok && r.result.ok ? "It works: " + root.flat(r.result.provider) + " answered." : root.flat(root.why(r));
        }, function (line) {
            if (line !== "")
                page.aiMessage = root.flat(line);
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
        id: aiDialog
        title: page.aiDialogProvider === "gemini" ? "Google Gemini" : "Your own server"
        subtitle: page.aiDialogProvider === "gemini" ? "Your Gemini key from Google AI Studio. It is kept in KWallet, never in a file, and is not shown again." : "Where the server is, and a key if it needs one. A key is kept in KWallet, never in a file, and for this address only: if you change the address, give the key again."
        standardButtons: Kirigami.Dialog.Cancel
        onClosed: {
            aiKey.text = ""; // a key that was typed and not saved is not kept anywhere
            page.aiPending = "";
            page.aiDialogError = "";
            page.syncProviderChoice();
        }
        customFooterActions: [
            Kirigami.Action {
                text: "Save"
                icon.name: "dialog-ok"
                enabled: !page.aiBusy
                onTriggered: page.saveAiDialog()
            }
        ]
        ColumnLayout {
            Kirigami.InlineMessage {
                Layout.fillWidth: true
                visible: page.aiDialogError !== ""
                type: Kirigami.MessageType.Error
                text: root.plain(page.aiDialogError)
            }
            QQC2.Label {
                text: "Model"
                textFormat: Text.PlainText
            }
            QQC2.TextField {
                id: aiModel
                Layout.fillWidth: true
            }
            QQC2.Label {
                visible: page.aiDialogProvider === "compatible"
                text: "Address of the server (https, or http on this computer)"
                textFormat: Text.PlainText
            }
            QQC2.TextField {
                id: aiBase
                visible: page.aiDialogProvider === "compatible"
                Layout.fillWidth: true
                placeholderText: "http://localhost:11434/v1"
            }
            QQC2.Label {
                text: page.aiHasKey && page.aiPending === "" ? "Key (one is saved; type a new one to replace it)" : "Key"
                textFormat: Text.PlainText
            }
            QQC2.TextField {
                id: aiKey
                Layout.fillWidth: true
                echoMode: TextInput.Password
                enabled: page.aiWalletOk
                placeholderText: page.aiWalletOk ? "" : "KWallet is not available"
            }
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
        title: "AI assistant (optional)"
    }
    FormCard.FormCard {
        FormCard.FormSwitchDelegate {
            id: aiSwitch
            text: "Suggest what an application may still need"
            description: "Off by default. When you press \"What else does this need?\" on an application, Cygnus reads that program's public documentation itself and asks an AI assistant what else it mentions (a group, a service, a library, a browser extension). Only the program's name and id, its format, and the text of those public pages are sent: nothing about you, your files or your other programs. The AI can be wrong, so Cygnus keeps only suggestions that quote a sentence really present in those pages, checks that a suggested package or group exists, labels each one as an unverified AI suggestion, and never installs anything without your approval."
            enabled: !page.changing && !page.aiBusy && !!page.aiState
            onToggled: page.changeAi(checked, page.aiState.provider, page.aiState.model, page.aiState.base_url || "")
        }
        FormCard.FormDelegateSeparator {}
        FormCard.FormComboBoxDelegate {
            id: aiProvider
            text: "Which assistant"
            description: page.aiProviderNote
            model: page.aiProviderNames
            enabled: !page.changing && !page.aiBusy && !!page.aiState
            onActivated: page.pickProvider(currentIndex)
        }
        FormCard.FormButtonDelegate {
            text: "Model, address and key…"
            description: page.aiProviderId === "compatible" ? "The server's address, the model it should use, and a key if it needs one." : "The model to use and your key."
            icon.name: "configure"
            visible: page.aiProviderId !== "hosted"
            enabled: !page.changing && !page.aiBusy && !!page.aiState
            onClicked: page.openAiDialog(page.aiProviderId)
        }
        FormCard.FormButtonDelegate {
            text: "Remove the saved key"
            icon.name: "edit-delete"
            visible: page.aiProviderId !== "hosted" && page.aiHasKey
            enabled: !page.changing && !page.aiBusy
            onClicked: page.removeAiKey()
        }
        FormCard.FormButtonDelegate {
            text: "Test the connection"
            description: "Checks that the assistant above answers. The shared assistant is only asked whether it is ready; the others get one harmless question. Nothing about you is sent."
            icon.name: "network-connect"
            enabled: !page.changing && !page.aiBusy && aiSwitch.checked
            onClicked: page.testAi()
        }
        FormCard.FormTextDelegate {
            visible: page.aiMessage !== ""
            text: "Note"
            description: page.aiMessage
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
        page.loadAi();
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
