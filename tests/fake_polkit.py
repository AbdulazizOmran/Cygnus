"""Fake polkit authority for helper tests (private session bus only).

Answers CheckAuthorization with ALLOW (env FAKE_POLKIT_ALLOW=1|0) and appends every request
(action id + polkit.message) as a JSON line to FAKE_POLKIT_LOG.
"""

import json
import os
import sys

import gi  # noqa: F401
from gi.repository import Gio, GLib

XML = """
<node><interface name="org.freedesktop.PolicyKit1.Authority">
  <method name="CheckAuthorization">
    <arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/><arg type="a{ss}" direction="in"/>
    <arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="(bba{ss})" direction="out"/>
  </method>
</interface></node>
"""


def main():
    allow = os.environ.get("FAKE_POLKIT_ALLOW", "1") == "1"
    log = os.environ["FAKE_POLKIT_LOG"]
    loop = GLib.MainLoop()

    def on_call(conn, sender, path, iface, method, params, invocation):
        subject, action, details, flags, _ = params.unpack()
        with open(log, "a") as f:
            f.write(json.dumps({"action": action, "message": details.get("polkit.message"),
                                "subject": subject[0], "interactive": bool(flags & 1)}) + "\n")
        invocation.return_value(GLib.Variant("((bba{ss}))", ((allow, False, {}),)))

    def on_bus(conn, name):
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        conn.register_object("/org/freedesktop/PolicyKit1/Authority", node.interfaces[0], on_call, None, None)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.PolicyKit1", Gio.BusNameOwnerFlags.NONE, on_bus,
                     lambda *a: print("ready", flush=True), lambda *a: loop.quit())
    loop.run()


if __name__ == "__main__":
    sys.exit(main())
