"""Build-tool name for ``open_brain.services.component_manifest_v2`` (decision D8).

The module object itself is re-bound so attribute patches reach the one shared parser.
"""

import sys

from open_brain.services import component_manifest_v2 as _shared
from open_brain.services.component_manifest_v2 import *  # noqa: F403

__all__ = [name for name in vars(_shared) if not name.startswith("_")]
sys.modules[__name__] = _shared
