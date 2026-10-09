"""One Cygnus window. When Cygnus is the default program for Flathub's Install button and software files, a second launch must
hand its arguments to the window that is already open, not open another one. They travel over the session bus under the
application's own name; the second launch exits as soon as the first one has taken them.

This is set up before any QML is loaded, so it cannot meet the Kirigami loading deadlock (see app.py)."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

SERVICE = "io.github.omranabdulaziz.Cygnus"
PATH = "/io/github/omranabdulaziz/Cygnus"
INTERFACE = "io.github.omranabdulaziz.Cygnus"
FORWARD_TIMEOUT_MS = 5000


class Receiver(QObject):
    """Exported on the bus. `opened` carries what a later launch asked for: {"args": [...], "cwd": "..."}."""

    opened = Signal(str)

    @Slot(str)
    def Open(self, payload: str) -> None:  # noqa: N802 - the D-Bus method name
        self.opened.emit(payload)


def listen(receiver: Receiver, bus: QDBusConnection | None = None) -> bool:
    """Become the one running instance. False when another already is. Without a session bus (a bare run) there is nothing
    to coordinate with, so this one simply is the only instance."""
    bus = bus or QDBusConnection.sessionBus()
    if not bus.isConnected():
        return True
    # The object first, then the name: a second launch that sees the name must find something to hand its arguments to.
    if not bus.registerObject(PATH, INTERFACE, receiver, QDBusConnection.RegisterOption.ExportAllSlots):
        return True  # no way to be reached: behave as the only instance rather than refuse to start
    if not bus.registerService(SERVICE):
        bus.unregisterObject(PATH)
        return False
    return True


def forward(args: list[str], cwd: str | None = None, bus: QDBusConnection | None = None) -> bool:
    """Hand `args` to the running instance. True when it took them."""
    bus = bus or QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    interface = QDBusInterface(SERVICE, PATH, INTERFACE, bus)
    if not interface.isValid():
        return False
    interface.setTimeout(FORWARD_TIMEOUT_MS)  # a window that has stopped answering is not waited for half a minute
    reply = interface.call("Open", json.dumps({"args": args, "cwd": cwd or os.getcwd()}))
    return reply.type() != QDBusMessage.MessageType.ErrorMessage
