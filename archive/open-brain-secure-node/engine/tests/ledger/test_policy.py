from __future__ import annotations

from open_brain_engine.ledger.policy import authorize_compartments, propagated_compartments


def test_compartments_require_every_label_and_spaces_do_not_grant_access() -> None:
    assert authorize_compartments({"private", "work"}, {"private", "work"})
    assert not authorize_compartments({"private"}, {"private", "work"})
    assert not authorize_compartments({"private"}, {"private", "work"}, spaces={"work"})


def test_propagated_compartments_are_a_deterministic_union() -> None:
    assert propagated_compartments(("work", "private"), ("private", "legal")) == (
        "legal",
        "private",
        "work",
    )
