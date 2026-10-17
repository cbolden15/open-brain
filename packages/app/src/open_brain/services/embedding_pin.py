"""Expected embedding inventory SHA-256 per platform, pinned into the frozen base (D8).

The source tree keeps the empty default. The native base build compiles a generated
replacement of this module (``tools.open_brain_dev.base_native.render_embedding_pin``)
from the audited embedding archive before freezing; it never rewrites this file.
"""

from __future__ import annotations

from collections.abc import Mapping

EXPECTED_EMBEDDING_INVENTORY_SHA256: Mapping[str, str] = {}
