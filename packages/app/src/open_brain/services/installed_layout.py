"""Map the resolved base executable to the installed product prefix.

Releases install the one-folder base at ``<prefix>/libexec/open-brain/open-brain`` with
``<prefix>/bin/open-brain`` as a relative symlink to it. PyInstaller's bootloader reports
the resolved executable as ``sys.executable`` when launched through that link. The
desktop bundles a copy of that folder with its links flattened into regular files at the same
``<prefix>/libexec/open-brain/open-brain``. A regular ``<prefix>/bin/open-brain`` is still accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

BASE_EXECUTABLE: Final = "libexec/open-brain/open-brain"
BASE_LINK: Final = "bin/open-brain"


def base_prefix(resolved: Path) -> Path | None:
    """Return the prefix of an already resolved base executable, or ``None``."""
    if not resolved.is_absolute() or resolved.name != "open-brain":
        return None
    parent = resolved.parent
    if parent.name == "bin":
        return parent.parent
    if parent.name == "open-brain" and parent.parent.name == "libexec":
        return parent.parent.parent
    return None


__all__ = ["BASE_EXECUTABLE", "BASE_LINK", "base_prefix"]
