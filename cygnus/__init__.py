"""Cygnus — application installer and manager for CachyOS / KDE Plasma."""

__version__ = "0.1.1"  # the single source of the version (pyproject reads it)

# Reverse-DNS application ID. It is only an identifier (the GitHub account is AbdulazizOmran); the bus names, polkit actions and
# file names are derived from it, so it is not renamed.
# Used for the desktop file, D-Bus names, polkit action IDs and AppStream.
APP_ID = "io.github.omranabdulaziz.Cygnus"
APP_NAME = "Cygnus"
