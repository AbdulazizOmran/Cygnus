from cygnus.core.desktop import entry

SAMPLE = """# comment
[Desktop Entry]
Version=1.0
Name=Helium
Name[ar]=هيليوم
GenericName=Web Browser
Exec=helium %U
Icon=helium
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
Comment=Line one\\nline two
Keywords=a\\;b;c;
Actions=new-window;new-private-window;

[Desktop Action new-window]
Name=New Window
Exec=helium
"""


def test_parse_values_and_locales():
    de = entry.parse(SAMPLE)
    assert de.get("Name") == "Helium"
    assert de.get("Name[ar]") == "هيليوم"
    assert de.get("Comment") == "Line one\nline two"
    assert de.get_list("Categories") == ["Network", "WebBrowser"]
    assert de.get_list("Keywords") == ["a;b", "c"]
    assert de.actions == ["new-window", "new-private-window"]
    assert de.get("Exec", group="Desktop Action new-window") == "helium"


def test_set_and_serialize_preserves_order():
    de = entry.parse(SAMPLE)
    de.set("Exec", "/home/me/.local/libexec/cygnus/launch/helium %U")
    de.set("X-Cygnus-Managed", "true")
    de.remove("Version")
    out = entry.parse(de.serialize())
    keys = [k for k, _ in out.groups[entry.MAIN_GROUP]]
    assert keys[0] == "Name" and keys[-1] == "X-Cygnus-Managed"
    assert out.get("Exec").startswith("/home/me/.local/libexec/cygnus/launch/helium")
    assert "Desktop Action new-window" in out.groups


def test_keys_outside_groups_are_ignored():
    de = entry.parse("Name=orphan\n[Desktop Entry]\nName=ok\n")
    assert de.get("Name") == "ok"
