"""Canonical annotation mutation validation and privacy-safe anchors."""

from dataclasses import replace
from uuid import uuid4

import pytest
from open_brain_engine.engine.browser_annotations import Annotation


def test_validate_quote_and_context_independently() -> None:
    reference = "https://example.org/synthetic"
    annotation = Annotation(str(uuid4()), "selected", "", " ", None)
    annotation.validate(reference)
    for field in ("quote", "prefix", "suffix", "note"):
        with pytest.raises(ValueError):
            replace(annotation, **{field: "password=synthetic-secret"}).validate(reference)
    with pytest.raises(ValueError):
        replace(annotation, prefix="contact@", quote="example.org", suffix="").validate(reference)
