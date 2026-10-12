"""What the Discover page says about the programs vendors publish as .deb / .rpm (the built-in sources in
cygnus.core.backends.vendor_feeds, and the ones a person added).

Everything here is written by hand, once: a name, one plain sentence, where the program belongs, and, where the same program is on Flathub,
its exact Flathub id. That id is the ONLY thing that lets Discover show a vendor's package and the Flatpak as one program: it is checked
against the real Flathub data before it is put here, and it is never guessed from a name. A program that is not on Flathub has none.
Google Chrome's beta channel has none on purpose: Flathub's `com.google.ChromeDev` is the developer ("unstable") channel, a different build.
A test keeps this list and the feeds in step: a feed without an entry here (or the other way round) fails it."""

from __future__ import annotations

from typing import Any

from cygnus.core.backends import vendor_feeds

# feed key -> (name, one sentence, freedesktop categories, developer, homepage, flathub id or "", theme icon)
_KNOWN: dict[str, tuple[str, str, tuple[str, ...], str, str, str, str]] = {
    "google-chrome-stable": ("Google Chrome", "Google's web browser.", ("Network", "WebBrowser"), "Google", "https://www.google.com/chrome/",
                             "com.google.Chrome", "google-chrome"),
    "google-chrome-beta": ("Google Chrome (Beta)", "Google's web browser, the beta channel: next release's features, less settled.",
                           ("Network", "WebBrowser"), "Google", "https://www.google.com/chrome/beta/", "", "google-chrome-beta"),
    "google-chrome-unstable": ("Google Chrome (Dev)", "Google's web browser, the developer channel: the newest features, the least settled.",
                               ("Network", "WebBrowser"), "Google", "https://www.google.com/chrome/dev/", "com.google.ChromeDev", "google-chrome-unstable"),
    "microsoft-edge-stable": ("Microsoft Edge", "Microsoft's web browser.", ("Network", "WebBrowser"), "Microsoft", "https://www.microsoft.com/edge",
                              "com.microsoft.Edge", "microsoft-edge"),
    "vivaldi-stable": ("Vivaldi", "A web browser with many options to arrange it your way.", ("Network", "WebBrowser"), "Vivaldi Technologies",
                       "https://vivaldi.com/", "com.vivaldi.Vivaldi", "vivaldi"),
    "brave-browser": ("Brave", "A web browser that blocks ads and trackers by default.", ("Network", "WebBrowser"), "Brave Software",
                      "https://brave.com/", "com.brave.Browser", "brave-browser"),
    "opera-stable": ("Opera", "A web browser with a built-in sidebar, messengers and a free VPN.", ("Network", "WebBrowser"), "Opera",
                     "https://www.opera.com/", "com.opera.Opera", "opera"),
    "signal-desktop": ("Signal", "Private messaging, on your computer.", ("Network", "InstantMessaging"), "Signal Foundation",
                       "https://signal.org/", "org.signal.Signal", "signal-desktop"),
    "1password": ("1Password", "A password manager.", ("Utility", "Security"), "AgileBits", "https://1password.com/",
                  "com.onepassword.OnePassword", "1password"),
    "teamviewer": ("TeamViewer", "Look at and control another computer from far away, or let someone do that to yours.",
                   ("Network", "RemoteAccess"), "TeamViewer", "https://www.teamviewer.com/", "", "teamviewer"),
    "slack-desktop": ("Slack", "Team chat for work.", ("Network", "InstantMessaging"), "Slack Technologies", "https://slack.com/",
                      "com.slack.Slack", "slack"),
    "code": ("Visual Studio Code", "A code editor from Microsoft, with extensions for most languages.", ("Development", "IDE", "TextEditor"),
             "Microsoft", "https://code.visualstudio.com/", "com.visualstudio.code", "visual-studio-code"),
    "code-insiders": ("Visual Studio Code Insiders", "The daily build of Visual Studio Code: newest features, can break.",
                      ("Development", "IDE", "TextEditor"), "Microsoft", "https://code.visualstudio.com/insiders/", "", "visual-studio-code-insiders"),
    "google-earth-pro-stable": ("Google Earth Pro", "Fly around the Earth and look at places from above.", ("Education", "Geography"), "Google",
                                "https://www.google.com/earth/", "com.google.EarthPro", "google-earth-pro"),
}
USER_SOURCE_TEXT = "A package list you added with `cygnus feed add`."


def vendors() -> list[dict[str, Any]]:
    """The entries Discover merges in (see discover.build_entries): the built-in vendors in a fixed order, then the sources a person
    added (a plain entry: nothing is known about those programs but the name they gave)."""
    mine = vendor_feeds.user_feeds()
    out: list[dict[str, Any]] = []
    for key in (*vendor_feeds.FEEDS, *sorted(k for k in mine if k not in vendor_feeds.FEEDS)):
        feed = mine.get(key) or vendor_feeds.FEEDS[key]
        formats = ["deb"] + (["rpm"] if feed.rpm else [])
        known = _KNOWN.get(key)  # a source of your own for a built-in program only changes where updates come from, not what it is
        if known is None:
            out.append({"package": key, "formats": formats, "name": key, "summary": USER_SOURCE_TEXT, "categories": ["Utility"],
                        "developer": "", "url": "", "flathub_id": "", "icon_name": "package-x-generic", "note": "Added by you."})
            continue
        name, summary, categories, developer, url, flathub_id, icon = known
        out.append({"package": key, "formats": formats, "name": name, "summary": summary, "categories": list(categories),
                    "developer": developer, "url": url, "flathub_id": flathub_id, "icon_name": icon, "note": ""})
    return out
