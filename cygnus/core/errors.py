"""Exception hierarchy for the Cygnus engine."""


class CygnusError(Exception):
    """Base class for all engine errors."""


class DetectionError(CygnusError):
    """The input could not be identified or is malformed."""


class UnsafeInputError(DetectionError):
    """The input is structurally hostile (path traversal, size bombs, ...)."""


class StorageError(CygnusError):
    """A storage location is unavailable or unusable."""


class RegistryError(CygnusError):
    """The application registry could not be opened or migrated."""


class ToolMissingError(CygnusError):
    """A required external tool is not installed."""

    def __init__(self, tool: str, package: str | None = None):
        self.tool = tool
        self.package = package
        hint = f" (install the '{package}' package)" if package else ""
        super().__init__(f"required tool '{tool}' is not installed{hint}")
