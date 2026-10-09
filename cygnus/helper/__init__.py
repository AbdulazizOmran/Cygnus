"""cygnus-helper: the privileged side of Cygnus (architecture §3.2, §16).

Runs as root, D-Bus activated, exits when idle. It computes plans itself, binds them to the
calling process, asks polkit with a summary it generated, and executes only allow-listed
commands (pacman, systemctl, gpasswd) with argv lists. It never runs shell strings.
"""

import os

BUS_NAME = "io.github.omranabdulaziz.Cygnus.Helper1"
OBJECT_PATH = "/io/github/omranabdulaziz/Cygnus/Helper1"
INTERFACE = BUS_NAME
ACTION_PREFIX = "io.github.omranabdulaziz.Cygnus"


def state_dir() -> str:
    return os.environ.get("CYGNUS_HELPER_STATE_DIR", "/var/lib/cygnus")


def staging_dir() -> str:
    return os.environ.get("CYGNUS_HELPER_STAGING_DIR", "/var/cache/cygnus/staging")
