"""What the Discover page says about the programs whose makers publish their own downloads on a GitHub release page (an AppImage, a .deb or
.rpm, an archive, an Arch package): the built-in list in cygnus.core.backends.release_feeds.

As in vendor_catalog.py, everything here is written by hand, once: a name, one plain sentence, where the program belongs, and, where the same
program is on Flathub, its exact Flathub id (the key of the entry in the real Flathub data: an application id without a ".desktop" suffix). That
id is the ONLY thing that lets Discover show a release download and the Flatpak as one program, and it is checked against the real Flathub data
before it is put here, never guessed from a name. A program that is not on Flathub has none. A test keeps this list and the project list in
step, and keeps it apart from the vendor list (a program is listed once)."""

from __future__ import annotations

from typing import Any

from cygnus.core.backends import release_feeds

# key -> (name, one sentence, freedesktop categories, developer, homepage, flathub id or "", theme icon)
_KNOWN: dict[str, tuple[str, str, tuple[str, ...], str, str, str, str]] = {
    "helium": ("Helium", "A privacy-minded web browser built on Chromium.", ("Network", "WebBrowser"), "imputnet",
               "https://github.com/imputnet/helium-linux", "", "helium"),
    "zen-browser": ("Zen Browser", "A calm, customisable web browser built on Firefox.", ("Network", "WebBrowser"), "Zen Browser",
                    "https://zen-browser.app", "app.zen_browser.zen", "zen-browser"),
    "obsidian": ("Obsidian", "Take notes in plain Markdown files and link them together.", ("Office",), "Obsidian", "https://obsidian.md/",
                 "md.obsidian.Obsidian", "obsidian"),
    "heroic": ("Heroic Games Launcher", "A launcher for your Epic, GOG and Amazon games.", ("Game",), "Heroic Games Launcher",
               "https://heroicgameslauncher.com", "com.heroicgameslauncher.hgl", "heroic"),
    "localsend": ("LocalSend", "Send files to other devices on your network, without the internet.", ("Network", "FileTransfer"), "LocalSend",
                  "https://localsend.org/", "org.localsend.localsend_app", "localsend"),
    "rustdesk": ("RustDesk", "Remote desktop: see and control another computer, or let someone use yours.", ("Network", "RemoteAccess"),
                 "RustDesk", "https://rustdesk.com", "com.rustdesk.RustDesk", "rustdesk"),
    "zettlr": ("Zettlr", "A Markdown editor for writing and research.", ("Office", "WordProcessor"), "Zettlr", "https://www.zettlr.com",
               "com.zettlr.Zettlr", "zettlr"),
    "logseq": ("Logseq", "Notes organised as an outline, kept in files on your computer.", ("Office",), "Logseq", "https://logseq.com",
               "com.logseq.Logseq", "logseq"),
    "joplin": ("Joplin", "Notes and to-do lists, with optional syncing.", ("Office",), "Joplin", "https://joplinapp.org/",
               "net.cozic.joplin_desktop", "joplin"),
    "ferdium": ("Ferdium", "All your messaging services in one window.", ("Network", "InstantMessaging"), "Ferdium", "https://ferdium.org",
                "org.ferdium.Ferdium", "ferdium"),
    "mailspring": ("Mailspring", "An email client.", ("Network", "Email"), "Foundry 376", "https://getmailspring.com/",
                   "com.getmailspring.Mailspring", "mailspring"),
    "beekeeper-studio": ("Beekeeper Studio", "A SQL editor and database manager.", ("Development", "Database"), "Beekeeper Studio",
                         "https://www.beekeeperstudio.io/", "io.beekeeperstudio.Studio", "beekeeper-studio"),
    "dbeaver": ("DBeaver Community", "A database tool for many kinds of databases.", ("Development", "Database"), "DBeaver", "https://dbeaver.io",
                "io.dbeaver.DBeaverCommunity", "dbeaver"),
    "anytype": ("Anytype", "Notes and wikis that stay on your device, with optional syncing.", ("Office",), "Anytype", "https://anytype.io/",
                "io.anytype.anytype", "anytype"),
    "cryptomator": ("Cryptomator", "Encrypt the files you keep in cloud storage.", ("Utility", "Security"), "Cryptomator", "https://cryptomator.org/",
                    "org.cryptomator.Cryptomator", "cryptomator"),
    "balena-etcher": ("balenaEtcher", "Write an operating-system image to a USB drive or an SD card.", ("System", "Utility"), "Balena",
                      "https://github.com/balena-io/etcher", "", "balena-etcher"),
    "tabby": ("Tabby", "A terminal with tabs, profiles and SSH.", ("System", "TerminalEmulator"), "Eugeny", "https://github.com/Eugeny/tabby",
              "", "tabby"),
    "cherry-studio": ("Cherry Studio", "A desktop app for chatting with many AI models.", ("Utility", "ArtificialIntelligence"), "Cherry Studio",
                      "https://cherry-ai.com/", "com.cherry_ai.CherryStudio", "cherry-studio"),
    "appflowy": ("AppFlowy", "Notes, wikis and projects, kept on your device.", ("Office", "ProjectManagement"), "AppFlowy", "https://appflowy.io",
                 "io.appflowy.AppFlowy", "appflowy"),
    "cemu": ("Cemu", "A Wii U emulator.", ("Game", "Emulator"), "Cemu project", "https://cemu.info", "info.cemu.Cemu", "cemu"),
    "losslesscut": ("LosslessCut", "Cut video and audio files without re-encoding them.", ("AudioVideo", "Video", "AudioVideoEditing"),
                    "Mikael Finstad", "https://losslesscut.app/", "no.mifi.losslesscut", "losslesscut"),
    "vesktop": ("Vesktop", "A Discord client with Vencord built in.", ("Network", "InstantMessaging"), "Vencord", "https://vesktop.dev/",
                "dev.vencord.Vesktop", "vesktop"),
    "ultimaker-cura": ("UltiMaker Cura", "Prepare 3D models for printing.", ("Graphics", "3DGraphics"), "UltiMaker",
                       "https://ultimaker.com/en/software/ultimaker-cura", "com.ultimaker.cura", "cura"),
    "freecad": ("FreeCAD", "Parametric 3D modelling.", ("Graphics", "3DGraphics", "Engineering"), "FreeCAD", "https://www.freecad.org/",
                "org.freecad.FreeCAD", "freecad"),
}


def releases() -> list[dict[str, Any]]:
    """The entries Discover merges in (see discover.build_entries), in the list's order."""
    out: list[dict[str, Any]] = []
    for key, project in release_feeds.PROJECTS.items():
        name, summary, categories, developer, url, flathub_id, icon = _KNOWN[key]
        out.append({"key": key, "formats": release_feeds.formats_of(project), "name": name, "summary": summary, "categories": list(categories),
                    "developer": developer, "url": url, "flathub_id": flathub_id, "icon_name": icon,
                    "page": f"https://github.com/{project.owner}/{project.repo}/releases"})
    return out
