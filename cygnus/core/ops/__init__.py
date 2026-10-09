"""Operations: plans turned into journaled executor steps (architecture §11)."""


def executor_for(registry):
    """An executor that knows every user-side step, so any journaled operation can run, be finished
    after an interruption, or be undone — whichever front end (CLI or GUI) started it."""
    from cygnus.core.ops import appimage_ops, flatpak_ops

    ex = appimage_ops.Ops(registry).executor()
    for kind, handler in flatpak_ops.HANDLERS.items():
        ex.register(kind, handler)
    return ex


def plan_uninstall(registry, installation_id: str, *, remove_payload: bool = False,
                   remove_adopted: list[str] = ()) -> list:
    from cygnus.core.errors import CygnusError
    from cygnus.core.ops import appimage_ops, flatpak_ops

    row = registry.conn.execute("SELECT format FROM installation WHERE id=?", (installation_id,)).fetchone()
    if row is None:
        raise CygnusError("unknown installation")
    if row[0] == "flatpak":
        return flatpak_ops.plan_uninstall(registry, installation_id)
    if row[0] == "appimage":
        return appimage_ops.plan_uninstall(registry, installation_id, remove_payload=remove_payload,
                                           remove_adopted=remove_adopted)
    raise CygnusError(f"{row[0]} applications are removed together with your system packages")
