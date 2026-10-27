# Review/MCP Friction (#126) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the six review/MCP onboarding frictions from issue cbolden15/open-brain#126 without changing any persisted format: stale update proposals become rejectable and are labelled, record reads disclose redaction, edits cannot write placeholders back, full-page updates show a diff, duplicate creates are refused at decision time, and the grant and retraction docs match the code.

**Architecture:** All decision semantics stay in the engine (`review.py`, `review_bound.py`, `records.py`, `paging.py`); `services/review_publication.py` maps engine failures to public codes once for CLI, MCP and the plugin bridge. Every new field is derived at read time (no schema, migration or stored status). The plugin bridge keeps its frozen protocol-1 DTOs by stripping the new review fields and folding the new error codes into `review_conflict`.

**Tech Stack:** Python 3.14, uv workspace, SQLite, pytest; TypeScript + vitest for one Obsidian plugin wire-grammar change.

Issue: <https://github.com/cbolden15/open-brain/issues/126>. Base: `origin/main` `c56f6ee`.

## Global Constraints

- SQLite schema version stays **23**, runtime session version stays **16**, Portable Brain version stays **18**. No migration, no new table or column, no new persisted status (no `superseded`).
- If a docs or test pin enumerates MCP tool argument schemas or error codes, update that pin in the same task.
- `t03_schema.json` is pinned byte-for-byte to `tests/fixtures/new-user-t03/strict-contract.schema.json` by `packages/engine/tests/test_t03_values.py::test_packaged_schema_matches_exact_frozen_grammar`. Any change to one is copied to the other in the same task (Task 7 does this). `frozen_contract_sha256` in `packages/app/tests/fixtures/t06-client-api.json` is a historical constant (it does not equal the current schema hash) and is not touched.
- Synthetic fixtures only. Never write private hostnames, absolute personal paths or personal data; test paths use `/srv/synthetic/...`.
- Run tests with `uv run --frozen pytest -q <path>`. Phase gates: `make verify`, `git diff --check`, `actionlint .github/workflows/ci.yml`.
- Commit with `git commit -F <file>` (no heredocs). Write the message with `printf` to `"$(git rev-parse --git-dir)/review-friction-msg.txt"`. Append the attribution trailer your harness requires. No push, no PR.
- The engine must not import the app. The app may import `open_brain_engine.engine.contracts` directly.
- Before editing any file, read the exact region named in the step. Line numbers below are from `c56f6ee` and drift as tasks land; match on the quoted text, not the number.
- After code edits, run `uv run --frozen ruff check --fix <changed .py files>` so import ordering matches the repo's isort rules, then re-run the task's tests.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `packages/engine/src/open_brain_engine/engine/contracts.py` | Modify | `ReviewBindingConflict`; `stale`/`replacement`/`page_diff` on review DTOs; projection marker helper; `ReviewTask.decide(allow_duplicate=)` |
| `packages/engine/src/open_brain_engine/engine/review_bound.py` | Modify | Name the changed binding; `target_head_advanced`; `duplicate_page_id` |
| `packages/engine/src/open_brain_engine/engine/review.py` | Modify | Reject skips live-state check; derived `stale`; redaction guard; page diff; duplicate refusal |
| `packages/engine/src/open_brain_engine/engine/records.py` | Modify | `ProjectedRecord.projection_applied` |
| `packages/engine/src/open_brain_engine/engine/paging.py` | Modify | Emit `projection_applied` in `record.read` / `history.show` |
| `packages/engine/src/open_brain_engine/engine/t03_schema.json` + `tests/fixtures/new-user-t03/strict-contract.schema.json` | Modify (identical) | Optional `projection_applied` boolean on both read responses |
| `packages/app/src/open_brain/services/review_publication.py` | Modify | Public codes, `binding` detail, `stale` in list rows, `allow_duplicate` argument |
| `packages/app/src/open_brain/services/local_entrypoints.py` | Modify | CLI messages, stale/diff text, `--allow-duplicate`, `mcp` help wording |
| `packages/app/src/open_brain/services/local_mcp.py` | Modify | Capture tool wording, `allow_duplicate` schema |
| `packages/app/src/open_brain/services/mcp_protocol.py` | Modify | Allowlist the three new error codes |
| `packages/app/src/open_brain/services/plugin_bridge.py` | Modify | Keep bridge DTOs frozen |
| `packages/obsidian-plugin/src/t03-wire.ts` | Modify | Accept optional `projection_applied` |
| `docs/review-publication.md`, `docs/records-and-history.md`, `docs/first-use.md`, `docs/install.md`, `README.md`, `docs/engineering/gotchas/README.md` | Modify | Behavior, grants table, dev-checkout note, coordinates, gotchas |

---

# Phase A — PR A on `fix/review-friction-a` (items 1, 4, 6, first-use coordinates)

Confirm the branch before starting:

```bash
cd "$(git rev-parse --show-toplevel)"   # the review-friction worktree
git status --short --branch   # expect: ## fix/review-friction-a...origin/main, clean
```

<!-- model: opus -->

### Task 1: Reject skips the live-state check; conflicts name the changed binding

The reject path was bound to the live target revision: `ReviewOperations._decide` (`review.py` ~932-936) runs `validate_current_binding` for every outcome, so a proposal whose target page advanced can never be rejected. This task gates that call on publishing outcomes and makes each binding mismatch say what changed.

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/contracts.py` (insert after `class EnrichmentUnavailable`, ~537)
- Modify: `packages/engine/src/open_brain_engine/engine/review_bound.py:20-27` (imports), `:495-503`, `:525-529`
- Modify: `packages/engine/src/open_brain_engine/engine/review.py:18-31` (imports), `:927-936`
- Modify: `docs/engineering/gotchas/README.md` (new REVIEW-006 at the top of `## Registry`)
- Test: `packages/app/tests/integration/engine/test_review_publication_engine.py`

**Interfaces:**
- Produces: `open_brain_engine.engine.contracts.ReviewBindingConflict(message: str, *, binding: str)`, a `ValueError` subclass with attribute `binding: str` in `{"source", "route", "draft", "target"}`. `str(error)` keeps the legacy text (`"review source state conflict"`, `"canonical page revision conflict"`, `"review digest conflict"`), so existing `pytest.raises(ValueError, match=...)` tests and the app's string map keep working.
- Produces: `binding="target"` is raised only when the published page head differs from the binding's `expected_publication_id`/`expected_page_sha256`. A manually edited or unreadable page still raises a plain `ValueError("canonical page revision conflict")`.

- [ ] **Step 1: Write the failing tests**

Add to the import block of `test_review_publication_engine.py`:

```python
from open_brain_engine.engine.contracts import ReviewBindingConflict
```

Change `test_bound_decision_refuses_corrupted_immutable_source_bytes` so its `pytest.raises` captures the error and checks the binding:

```python
    with pytest.raises(ReviewBindingConflict, match="review source state conflict") as caught:
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.corrupt.decision",
            expected_review_digest=proposal.review_digest,
        )
    assert caught.value.binding == "source"
```

Append two tests:

```python
def test_reject_clears_an_update_whose_target_advanced(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, second = _sources(engine)
    third = _text_sources(engine, 1, prefix="stale")[0]
    created = engine.review.propose(
        (first,), (ProposalDraft("Stable", "Root"),), delivery_id="delivery.stale.root"
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.stale.root.approve",
        expected_review_digest=created.review_digest,
    )
    winner, stale = (
        engine.review.propose(
            (source,),
            (ProposalDraft("Stable", body),),
            delivery_id=f"delivery.stale.{body}",
            target_page_id=created.page_id,
        )[0]
        for source, body in ((second, "Winner"), (third, "Loser"))
    )
    engine.review.decide(
        winner.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.stale.winner.approve",
        expected_review_digest=winner.review_digest,
    )
    page = next((root / "content" / "spaces").rglob(f"{created.page_id}.md"))
    published = page.read_bytes()

    for outcome, edited in (
        (DecisionOutcome.APPROVED, None),
        (DecisionOutcome.EDITED, "Edited loser"),
    ):
        with pytest.raises(ReviewBindingConflict, match="canonical page revision conflict") as caught:
            engine.review.decide(
                stale.proposal_id,
                outcome,
                delivery_id=f"delivery.stale.loser.{outcome.value}",
                edited_markdown=edited,
                expected_review_digest=stale.review_digest,
            )
        assert caught.value.binding == "target"

    rejected = engine.review.decide(
        stale.proposal_id,
        DecisionOutcome.REJECTED,
        delivery_id="delivery.stale.loser.reject",
        expected_review_digest=stale.review_digest,
    )
    assert rejected.outcome is DecisionOutcome.REJECTED
    assert rejected.publication_id is None
    assert page.read_bytes() == published
    assert engine.review.show(stale.proposal_id).status == "rejected"


def test_binding_conflicts_name_the_draft_and_route(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,), (ProposalDraft("Named", "Named binding"),), delivery_id="delivery.named"
    )[0]
    with pytest.raises(ReviewBindingConflict, match="review digest") as token_conflict:
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.named.token",
            expected_review_digest="0" * 64,
        )
    assert token_conflict.value.binding == "draft"

    other = engine.inbox.create_space("Elsewhere", delivery_id="delivery.named.space")
    engine.inbox.route(first, other.space_id, delivery_id="delivery.named.route")
    with pytest.raises(ReviewBindingConflict, match="review source state conflict") as moved:
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.named.approve",
            expected_review_digest=proposal.review_digest,
        )
    assert moved.value.binding == "route"
    rejected = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.REJECTED,
        delivery_id="delivery.named.reject",
        expected_review_digest=proposal.review_digest,
    )
    assert rejected.outcome is DecisionOutcome.REJECTED
    assert tuple((root / "content" / "spaces").rglob(f"{proposal.page_id}.md")) == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py`
Expected: FAIL with `ImportError: cannot import name 'ReviewBindingConflict'`.

- [ ] **Step 3: Add `ReviewBindingConflict` to `contracts.py`**

Insert directly after the `EnrichmentUnavailable` class (before `@dataclass(frozen=True, slots=True)` / `class ProposalRecord:`):

```python
_REVIEW_BINDINGS = frozenset({"source", "route", "draft", "target"})


class ReviewBindingConflict(ValueError):
    """A frozen review binding no longer matches live state.

    The message keeps the established conflict text; ``binding`` names the part
    that changed: ``source``, ``route``, ``draft`` or ``target``.
    """

    def __init__(self, message: str, *, binding: str) -> None:
        if binding not in _REVIEW_BINDINGS:
            raise ValueError("invalid review binding")
        self.binding = binding
        super().__init__(message)
```

- [ ] **Step 4: Raise it from `review_bound.validate_current_binding`**

Add `ReviewBindingConflict` to the `from .contracts import (...)` block at the top of `review_bound.py`.

Replace the per-capture check (currently one `if` with three `or` clauses ending in `raise ValueError("review source state conflict")`):

```python
        for capture in captures:
            capture_id = cast(str, capture["capture_id"])
            frozen = state_by_id[capture_id]
            if (
                capture["space_id"] != frozen["space_id"]
                or _latest_route_id(connection, capture_id) != frozen["route_id"]
            ):
                raise ReviewBindingConflict("review source state conflict", binding="route")
            if sha256(_source_record_bytes(engine, capture)).hexdigest() != frozen["sha256"]:
                raise ReviewBindingConflict("review source state conflict", binding="source")
```

Replace the head comparison in the `if binding["operation"] == "update":` branch:

```python
            if (
                current_publication_id != binding["expected_publication_id"]
                or current_sha256 != binding["expected_page_sha256"]
            ):
                raise ReviewBindingConflict("canonical page revision conflict", binding="target")
```

Leave every other `raise ValueError("canonical page revision conflict")` in this function unchanged (unreadable or manually edited page, create path already exists).

- [ ] **Step 5: Gate the live-state check on publishing outcomes in `review.py`**

Add `ReviewBindingConflict` to the `from .contracts import (...)` block. Replace the token check and the pre-transaction binding check in `_decide`:

```python
        if (
            expected_review_digest is not None
            and expected_review_digest != inspection.review_digest
        ):
            raise ReviewBindingConflict("review digest conflict", binding="draft")
        if (
            context is not None and reserved is None
            and outcome is not DecisionOutcome.REJECTED
            and review_binding_base_version(context[1]) != 5
        ):
            # Rejection writes no page, so a source, route or target that moved
            # after drafting must not strand the proposal. Facet (v5) bindings
            # keep their transactional check because it also enforces authority.
            validate_current_binding(self, context[1])
```

Do not change the v5 call inside the transaction (`if context is not None and review_binding_base_version(context[1]) == 5:`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/engine/test_review_patches.py packages/app/tests/integration/engine/test_review_routing.py`
Expected: all pass (existing `match="canonical page revision conflict"` and `match="review source state conflict"` assertions still match the subclass).

- [ ] **Step 7: Record the gotcha**

Insert at the top of `## Registry` in `docs/engineering/gotchas/README.md` (above `### DOCS-005`):

```markdown
### REVIEW-006: Rejecting a proposal was bound to the live target revision

Category: Review / Publication.

Symptom: a full-page update proposal whose target page advanced after drafting
stayed `pending` forever. `review reject` and `brain_review_reject` returned
`review_conflict`, because `ReviewOperations._decide` ran
`validate_current_binding` for every outcome, including `REJECTED`, and that
check compares the live page head with the binding. A fresh `review show`
returned the same token and binding, so "inspect again" could not help.

Action: rejection now skips the live source, route and target check for
non-facet bindings; it writes no page. Approve and edit-and-approve still run
it, and a moved page head raises `ReviewBindingConflict(binding="target")`,
which the app reports as `review_target_stale`. Facet (binding v5) proposals
still validate on reject because `validate_facet_current_binding` also enforces
query and subject authority. When adding a check to `_decide`, decide per
outcome whether it protects a write.

Discovered: 2026-10-27, issue #126.
```

- [ ] **Step 8: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/contracts.py \
  packages/engine/src/open_brain_engine/engine/review_bound.py \
  packages/engine/src/open_brain_engine/engine/review.py \
  packages/app/tests/integration/engine/test_review_publication_engine.py \
  docs/engineering/gotchas/README.md
printf '%s\n' 'fix(review): let reject clear proposals whose target advanced' '' \
  'Rejection writes no page, so it no longer runs the live source, route and' \
  'target check. Binding conflicts now name the binding that changed.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: opus -->

### Task 2: Derived `stale` flag on `review show` and `review list`

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/contracts.py` (`ProposalRecord` ~541-556, `ReviewProposal` ~570-590)
- Modify: `packages/engine/src/open_brain_engine/engine/review_bound.py` (new function after `validate_current_binding`)
- Modify: `packages/engine/src/open_brain_engine/engine/review.py` (`_list_proposals` ~609-711, `_show_proposal` ~732-878, imports ~49-58)
- Modify: `packages/app/src/open_brain/services/review_publication.py` (`_proposal_summary` ~305-317)
- Modify: `packages/app/src/open_brain/services/plugin_bridge.py` (publication handler ~619-645, new module helpers)
- Modify: `docs/engineering/gotchas/README.md` (new OBSIDIAN-007 at the top)
- Test: `packages/app/tests/integration/engine/test_review_publication_engine.py`, `packages/app/tests/integration/services/test_review_publication.py`, `packages/app/tests/unit/test_plugin_bridge.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the existing engine.
- Produces: `ProposalRecord.stale: bool = False`, `ReviewProposal.stale: bool = False` (last fields, default `False`). `review_bound.target_head_advanced(connection: sqlite3.Connection, binding: Mapping[str, object]) -> bool`. `stale` is `True` only for a `pending` update proposal whose `review_page_heads` row has a different `publication_id` or `published_sha256` than the binding. It reads no files.
- Produces: `plugin_bridge._BRIDGE_HIDDEN_REVIEW_FIELDS: frozenset[str]` and `plugin_bridge._frozen_publication_result(result: dict[str, object]) -> dict[str, object]`. Task 9 extends the set.

- [ ] **Step 1: Write the failing tests**

Engine test, append to `test_review_publication_engine.py`:

```python
def test_show_and_list_derive_stale_for_an_advanced_update_target(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    third = _text_sources(engine, 1, prefix="derived")[0]
    created = engine.review.propose(
        (first,), (ProposalDraft("Stable", "Root"),), delivery_id="delivery.derived.root"
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.derived.root.approve",
        expected_review_digest=created.review_digest,
    )
    winner, loser = (
        engine.review.propose(
            (source,),
            (ProposalDraft("Stable", body),),
            delivery_id=f"delivery.derived.{body}",
            target_page_id=created.page_id,
        )[0]
        for source, body in ((second, "Winner"), (third, "Loser"))
    )

    assert engine.review.show(loser.proposal_id).stale is False
    assert {row.proposal_id: row.stale for row in engine.review.list(status="pending")} == {
        winner.proposal_id: False,
        loser.proposal_id: False,
    }
    engine.review.decide(
        winner.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.derived.winner.approve",
        expected_review_digest=winner.review_digest,
    )
    assert engine.review.show(loser.proposal_id).stale is True
    assert {row.proposal_id: row.stale for row in engine.review.list(status="pending")} == {
        loser.proposal_id: True
    }
    assert engine.review.show(winner.proposal_id).stale is False
    assert engine.review.show(created.proposal_id).stale is False
```

Service test, append to `test_review_publication.py`:

```python
def test_list_and_show_surface_derived_staleness() -> None:
    service, task = _service()
    task.list.return_value = (replace(_record(), stale=True),)
    listed = service.list({})
    assert cast(list[dict[str, object]], listed["proposals"])[0]["stale"] is True
    task.show.return_value = replace(_detail(), stale=True)
    assert service.show({"proposal_id": PROPOSAL})["stale"] is True
```

Bridge test, append to `packages/app/tests/unit/test_plugin_bridge.py`:

```python
def test_bridge_hides_review_fields_added_after_protocol_one() -> None:
    hide = plugin_bridge_module._frozen_publication_result
    assert hide({"status": "shown", "proposal_id": "p", "stale": True}) == {
        "status": "shown",
        "proposal_id": "p",
    }
    listed: dict[str, object] = {
        "status": "listed",
        "proposals": [{"proposal_id": "p", "stale": False}],
        "offset": 0,
        "next_offset": None,
    }
    assert hide(listed) == {
        "status": "listed",
        "proposals": [{"proposal_id": "p"}],
        "offset": 0,
        "next_offset": None,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py::test_show_and_list_derive_stale_for_an_advanced_update_target packages/app/tests/integration/services/test_review_publication.py::test_list_and_show_surface_derived_staleness packages/app/tests/unit/test_plugin_bridge.py::test_bridge_hides_review_fields_added_after_protocol_one`
Expected: FAIL (`AttributeError: ... 'stale'`, `TypeError: ... unexpected keyword 'stale'`, `AttributeError: ... '_frozen_publication_result'`).

- [ ] **Step 3: Add the DTO fields**

In `contracts.py`, append to `ProposalRecord` after `draft_type: str = "full_page"`:

```python
    stale: bool = False
```

Append to `ReviewProposal` after `facet_binding_json: str | None = None`:

```python
    stale: bool = False
```

- [ ] **Step 4: Add `target_head_advanced` to `review_bound.py`**

Insert after `validate_current_binding`:

```python
def target_head_advanced(connection: sqlite3.Connection, binding: Mapping[str, object]) -> bool:
    """Return whether a reviewed update's target page head moved; reads no page files."""
    if binding["operation"] != "update":
        return False
    head = connection.execute(
        "SELECT publication_id, published_sha256 FROM review_page_heads WHERE page_id = ?",
        (binding["page_id"],),
    ).fetchone()
    if head is None:
        return False
    return (
        head["publication_id"] != binding["expected_publication_id"]
        or head["published_sha256"] != binding["expected_page_sha256"]
    )
```

A missing head row means no review approval has published the page since a legacy publication, so the target has not advanced through review.

- [ ] **Step 5: Derive `stale` in `review.py`**

Add `target_head_advanced` to the `from .review_bound import (...)` block.

In `_list_proposals`, inside the `try:` block, directly after the `references_by_proposal = {...}` comprehension and before `finally:`:

```python
            stale_ids: set[str] = set()
            for row in rows:
                bound = contexts[cast(str, row["proposal_id"])]
                if (
                    bound is not None
                    and cast(str, row["status"]) == "pending"
                    and target_head_advanced(connection, bound[1])
                ):
                    stale_ids.add(cast(str, row["proposal_id"]))
```

In the `ProposalRecord(...)` constructor at the end of the method, add after `draft_type=draft_type,`:

```python
                    stale=proposal_id in stale_ids,
```

In `_show_proposal`, inside the `try:` block directly after `references = tuple(...)` and before `finally:`:

```python
            stale = (
                binding is not None
                and cast(str, proposal["status"]) == "pending"
                and target_head_advanced(connection, binding)
            )
```

In the `return ReviewProposal(...)` call, add after `facet_binding_json=(...)`:

```python
            stale=stale,
```

- [ ] **Step 6: Surface `stale` in list rows**

In `review_publication.py` `_proposal_summary`, add after `"terminal_decision_id": record.terminal_decision_id,`:

```python
        "stale": record.stale,
```

`service.show` already returns every `ReviewProposal` field through `asdict`, so `stale` reaches MCP and CLI JSON without further change.

- [ ] **Step 7: Keep the plugin bridge DTO frozen**

The plugin parses facet `publication.show` and `publication.list` rows with exact key sets (`packages/obsidian-plugin/src/facet-inspection.ts:85`, `facet-publication.ts:294`), and `test_bridge_uses_shared_organization_and_publication_services` pins the show key set to `packages/app/tests/fixtures/t06-client-api.json`. Add near the other module constants in `plugin_bridge.py`:

```python
# Review fields added after bridge protocol 1 froze. The plugin's exact-key
# parsers reject unknown keys, so the bridge withholds them (OBSIDIAN-007).
_BRIDGE_HIDDEN_REVIEW_FIELDS = frozenset({"stale"})


def _frozen_publication_result(result: dict[str, object]) -> dict[str, object]:
    visible = {
        key: value for key, value in result.items() if key not in _BRIDGE_HIDDEN_REVIEW_FIELDS
    }
    rows = visible.get("proposals")
    if isinstance(rows, list):
        visible["proposals"] = [
            {
                key: value
                for key, value in cast(dict[str, object], row).items()
                if key not in _BRIDGE_HIDDEN_REVIEW_FIELDS
            }
            for row in rows
        ]
    return visible
```

In the `publication_handlers` branch, change the successful return:

```python
            if operation == "publication.propose":
                runtime.commit_review(_plugin_result_size(request_id, publication_result))
            if operation in {"publication.show", "publication.list"}:
                publication_result = _frozen_publication_result(publication_result)
            return publication_result
```

- [ ] **Step 8: Record the gotcha**

Insert at the top of `## Registry` (above REVIEW-006):

```markdown
### OBSIDIAN-007: Bridge publication DTOs are exact-key; shared review fields must be withheld

Category: Obsidian plugin / Wire compatibility.

Symptom: `ReviewPublicationService.show` and `list` serve MCP, the CLI and the
plugin bridge. The plugin parses facet `publication.show` responses and
`publication.list` rows with `exact(...)` (`facet-inspection.ts`,
`facet-publication.ts`), and
`test_bridge_uses_shared_organization_and_publication_services` pins the show
key set to `t06-client-api.json`. Adding an agent-facing field such as `stale`
to the shared output breaks facet publication in the plugin. New error codes
fail the same way: `PluginBridgeFailure` raises on any code outside its
allowlist.

Action: `plugin_bridge._frozen_publication_result` strips
`_BRIDGE_HIDDEN_REVIEW_FIELDS`, and `_BRIDGE_REVIEW_CODES` folds new review
codes into `review_conflict` before `PluginBridgeFailure`. Add each new
agent-facing review field or code there unless the plugin parsers change in the
same commit.

Discovered: 2026-10-27, issue #126.
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/services/test_review_publication.py packages/app/tests/unit/test_plugin_bridge.py`
Expected: all pass, including the existing fixture-shape test for `publication.show`.

- [ ] **Step 10: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/contracts.py \
  packages/engine/src/open_brain_engine/engine/review_bound.py \
  packages/engine/src/open_brain_engine/engine/review.py \
  packages/app/src/open_brain/services/review_publication.py \
  packages/app/src/open_brain/services/plugin_bridge.py \
  packages/app/tests/integration/engine/test_review_publication_engine.py \
  packages/app/tests/integration/services/test_review_publication.py \
  packages/app/tests/unit/test_plugin_bridge.py docs/engineering/gotchas/README.md
printf '%s\n' 'feat(review): derive a stale flag for update proposals' '' \
  'review show and review list report stale when the target page head moved' \
  'after drafting. Derived at read time; nothing is stored. The plugin bridge' \
  'keeps its protocol-1 shape.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: sonnet:high -->

### Task 3: `review_target_stale` code, binding detail and CLI text

**Files:**
- Modify: `packages/app/src/open_brain/services/review_publication.py:57-62`, `:345-383`
- Modify: `packages/app/src/open_brain/services/local_entrypoints.py:780-781` (except clause), `:1920-1935` (`_run_review` list/show text), `:1943-1963` (`_write_review_failure`)
- Modify: `packages/app/src/open_brain/services/mcp_protocol.py` (~254 allowlist)
- Modify: `packages/app/src/open_brain/services/plugin_bridge.py` (~644 except clause; new constant)
- Test: `packages/app/tests/integration/services/test_review_publication.py`, `packages/app/tests/integration/services/test_local_mcp.py`, `packages/app/tests/integration/services/test_local_entrypoints.py`, `packages/app/tests/unit/test_plugin_bridge.py`

**Interfaces:**
- Consumes: `ReviewBindingConflict` (Task 1), `stale` on list rows and show output (Task 2), `plugin_bridge._frozen_publication_result` (Task 2).
- Produces: `ReviewPublicationError(code: str, *, binding: str | None = None)` with attribute `binding`. Public code `review_target_stale` (binding `"target"`). `review_conflict` carries `binding` in `{"source", "route", "draft"}` or `None`. `plugin_bridge._BRIDGE_REVIEW_CODES: dict[str, str]`. `_write_review_failure(code, *, json_output, binding=None)`.

- [ ] **Step 1: Write the failing tests**

Service tests, append to `test_review_publication.py` (extend the existing `from open_brain_engine.engine.contracts import DecisionRecord` to `import DecisionRecord, ReviewBindingConflict`):

```python
@pytest.mark.parametrize(
    ("error", "code", "binding"),
    (
        (
            ReviewBindingConflict("canonical page revision conflict", binding="target"),
            "review_target_stale",
            "target",
        ),
        (
            ReviewBindingConflict("review source state conflict", binding="route"),
            "review_conflict",
            "route",
        ),
        (
            ReviewBindingConflict("review source state conflict", binding="source"),
            "review_conflict",
            "source",
        ),
        (ReviewBindingConflict("review digest conflict", binding="draft"), "review_conflict", "draft"),
        (ValueError("canonical page revision conflict"), "review_conflict", None),
    ),
)
def test_decision_conflicts_report_the_changed_binding(
    error: ValueError, code: str, binding: str | None
) -> None:
    service, task = _service()
    task.decide.side_effect = error
    with pytest.raises(ReviewPublicationError) as caught:
        service.approve({"proposal_id": PROPOSAL, "review_token": DIGEST})
    assert (caught.value.code, caught.value.binding) == (code, binding)


def test_stale_update_is_rejectable_but_not_approvable(tmp_path: Path) -> None:
    tasks = open_local_engine(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Projects",))
    )
    space_id = tasks.spaces.spaces()[0].space_id
    root_source, winner_source, stale_source = (
        tasks.capture.accept(
            TextPayload(f"Stale source {index}"),
            delivery_id=f"review.service.stale.{index}",
            space_id=space_id,
        ).capture_id
        for index in range(3)
    )
    service = ReviewPublicationService(tasks.review)
    created = service.propose(
        {"capture_ids": [root_source], "title": "Stale target", "markdown": "Root body"}
    )
    service.approve(
        {
            "proposal_id": created["proposal_id"],
            "review_token": service.show({"proposal_id": created["proposal_id"]})["review_token"],
        }
    )
    winner, stale = (
        service.propose(
            {
                "capture_ids": [source],
                "title": "Stale target",
                "markdown": body,
                "target_page_id": created["page_id"],
            }
        )
        for source, body in ((winner_source, "Winner body"), (stale_source, "Stale body"))
    )
    service.approve(
        {
            "proposal_id": winner["proposal_id"],
            "review_token": service.show({"proposal_id": winner["proposal_id"]})["review_token"],
        }
    )
    shown = service.show({"proposal_id": stale["proposal_id"]})
    assert shown["stale"] is True
    with pytest.raises(ReviewPublicationError, match="^review_target_stale$"):
        service.approve({"proposal_id": stale["proposal_id"], "review_token": shown["review_token"]})
    rejected = service.reject(
        {"proposal_id": stale["proposal_id"], "review_token": shown["review_token"]}
    )
    assert rejected["status"] == "rejected"
    assert service.list({"status": "pending"})["proposals"] == []
```

MCP transport test, append to `test_local_mcp.py` (add `from open_brain.services.review_publication import ReviewPublicationError`):

```python
@pytest.mark.parametrize("code", ["review_target_stale"])
def test_review_safe_errors_survive_mcp_transport(code: str) -> None:
    def fail(_arguments: Mapping[str, object]) -> dict[str, object]:
        raise ReviewPublicationError(code)

    adapter = LocalMcpAdapter(review_approve=fail)
    reply = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_review_approve",
            {"proposal_id": REVIEW_PROPOSAL, "review_token": REVIEW_DIGEST},
        ),
    )[1]
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"] == [{"type": "text", "text": code}]
```

CLI test, append to `test_local_entrypoints.py`:

```python
def test_owner_review_cli_names_a_stale_target_and_still_rejects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform_name: str
) -> None:
    home = _private_home(tmp_path)
    brain = home / "brain"
    tasks = open_local_engine(compile_single_user_local(brain))
    space = tasks.spaces.create_space("Projects", delivery_id="review.cli.stale.space")
    root_source, winner_source, stale_source = (
        tasks.capture.accept(
            TextPayload(f"Stale CLI source {index}"),
            delivery_id=f"review.cli.stale.capture.{index}",
            space_id=space.space_id,
        ).capture_id
        for index in range(3)
    )
    created = tasks.review.propose(
        (root_source,), (ProposalDraft("Stale CLI", "Root"),), delivery_id="review.cli.stale.root"
    )[0]
    tasks.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="review.cli.stale.root.approve",
        expected_review_digest=created.review_digest,
    )
    winner, stale = (
        tasks.review.propose(
            (source,),
            (ProposalDraft("Stale CLI", body),),
            delivery_id=f"review.cli.stale.{body}",
            target_page_id=created.page_id,
        )[0]
        for source, body in ((winner_source, "Winner"), (stale_source, "Loser"))
    )
    tasks.review.decide(
        winner.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="review.cli.stale.winner",
        expected_review_digest=winner.review_digest,
    )

    def cli(*arguments: str) -> int:
        return run_cli(
            (*arguments, "--data-dir", str(brain)),
            environment={"HOME": str(home)},
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )

    token = cast(str, stale.review_digest)
    assert cli("review", "approve", stale.proposal_id, "--review-token", token, "--json") == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"]["code"] == "review_target_stale"
    assert failure["error"]["binding"] == "target"
    assert cli("review", "approve", stale.proposal_id, "--review-token", token) == 1
    assert "can only be rejected" in capsys.readouterr().err
    assert cli("review", "show", stale.proposal_id) == 0
    assert "can only be rejected" in capsys.readouterr().out
    assert cli("review", "list", "--status", "pending") == 0
    assert f"{stale.proposal_id}  [pending, stale]" in capsys.readouterr().out
    assert cli("review", "reject", stale.proposal_id, "--review-token", token, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
```

Bridge test, append to `test_plugin_bridge.py`:

```python
def test_bridge_folds_new_review_codes_into_review_conflict() -> None:
    codes = plugin_bridge_module._BRIDGE_REVIEW_CODES
    assert codes["review_target_stale"] == "review_conflict"
    for code in codes.values():
        assert PluginBridgeFailure(code).code == code
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/services/test_review_publication.py packages/app/tests/integration/services/test_local_mcp.py::test_review_safe_errors_survive_mcp_transport packages/app/tests/integration/services/test_local_entrypoints.py::test_owner_review_cli_names_a_stale_target_and_still_rejects packages/app/tests/unit/test_plugin_bridge.py::test_bridge_folds_new_review_codes_into_review_conflict`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'binding'`, `tool call failed` text, CLI code `review_conflict`, missing `_BRIDGE_REVIEW_CODES`).

- [ ] **Step 3: Map engine conflicts in `review_publication.py`**

Extend the engine import with `from open_brain_engine.engine.contracts import ReviewBindingConflict`. Replace `ReviewPublicationError`:

```python
class ReviewPublicationError(ValueError):
    """One bounded public failure without draft or source content."""

    def __init__(self, code: str, *, binding: str | None = None) -> None:
        self.code = code
        self.binding = binding
        super().__init__(code)
```

At the top of `_public_error`, after the `FacetError`/`T03Error` branch:

```python
    if isinstance(error, ReviewBindingConflict):
        if error.binding == "target":
            return ReviewPublicationError("review_target_stale", binding="target")
        return ReviewPublicationError("review_conflict", binding=error.binding)
```

Leave the string map unchanged; it still covers plain `ValueError`s.

- [ ] **Step 4: CLI failure text and stale markers in `local_entrypoints.py`**

Change the except clause in the main dispatcher:

```python
    except ReviewPublicationError as error:
        return _write_review_failure(error.code, json_output=json_output, binding=error.binding)
```

Replace `_write_review_failure`:

```python
def _write_review_failure(code: str, *, json_output: bool, binding: str | None = None) -> int:
    if code == "invalid_arguments":
        _write_usage_failure(json_output=json_output)
        return 2
    messages = {
        "idempotency_conflict": "This retry key belongs to a different review operation.",
        "unknown_proposal": "Review proposal not found. List proposals again.",
        "unknown_capture": "One or more selected captures were not found.",
        "unknown_page": "The target canonical page was not found.",
        "duplicate_source": "Each selected capture ID must be unique.",
        "mixed_source_spaces": "All selected captures must be routed to the same space.",
        "source_unrouted": "Every selected capture must be routed before proposing publication.",
        "terminal_decision": "This proposal already has a terminal decision.",
        "review_conflict": "The reviewed source, route, draft, or target changed. Inspect again.",
        "review_target_stale": (
            "The target page changed after this proposal was drafted. The proposal can only be "
            "rejected; propose a new update from the current page."
        ),
        "response_too_large": "The complete projected proposal exceeds the inspection limit.",
    }
    binding_messages = {
        "source": "A selected source changed after inspection. Inspect again.",
        "route": "A selected source was rerouted after inspection. Inspect again.",
        "draft": "The review token does not match this proposal. Inspect again and use its token.",
    }
    message = messages.get(code, "Open Brain could not complete the review command.")
    if code == "review_conflict" and binding is not None and binding in binding_messages:
        message = binding_messages[binding]
    failure: dict[str, object] = {"code": code, "message": message}
    if binding is not None:
        failure["binding"] = binding
    if json_output:
        _write_json({"status": "failed", "error": failure})
    else:
        print(message, file=sys.stderr)
    return 1
```

In `_run_review`, change the list row print and add a stale line to show:

```python
        for row in rows:
            marker = ", stale" if row.get("stale") else ""
            print(
                _terminal_text(
                    f"{row['proposal_id']}  [{row['status']}{marker}]  "
                    f"{row['operation']}  {row['title']}"
                )
            )
```

```python
        if result.get("projection_applied"):
            print("Privacy projection replaced protected material.")
        if result.get("stale"):
            print(
                "The target page changed after this proposal was drafted; "
                "it can only be rejected."
            )
```

- [ ] **Step 5: Allowlist the MCP code**

In `mcp_protocol.py`, add `"review_target_stale",` directly after `"review_conflict",` in the safe-message set.

- [ ] **Step 6: Fold the code at the plugin bridge**

In `plugin_bridge.py`, below `_frozen_publication_result`:

```python
# Review codes added after bridge protocol 1 froze; PluginBridgeFailure rejects
# codes outside its allowlist (OBSIDIAN-007).
_BRIDGE_REVIEW_CODES = {"review_target_stale": "review_conflict"}
```

Change the publication except clause:

```python
        except ReviewPublicationError as error:
            raise PluginBridgeFailure(
                _BRIDGE_REVIEW_CODES.get(error.code, error.code)
            ) from None
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/services/test_review_publication.py packages/app/tests/integration/services/test_local_mcp.py packages/app/tests/integration/services/test_local_entrypoints.py -k "review" packages/app/tests/unit/test_plugin_bridge.py`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add packages/app/src/open_brain/services/review_publication.py \
  packages/app/src/open_brain/services/local_entrypoints.py \
  packages/app/src/open_brain/services/mcp_protocol.py \
  packages/app/src/open_brain/services/plugin_bridge.py \
  packages/app/tests/integration/services/test_review_publication.py \
  packages/app/tests/integration/services/test_local_mcp.py \
  packages/app/tests/integration/services/test_local_entrypoints.py \
  packages/app/tests/unit/test_plugin_bridge.py
printf '%s\n' 'feat(review): report review_target_stale and the changed binding' '' \
  'Approving a proposal whose target page moved now returns review_target_stale' \
  'and says it can only be rejected. The CLI names the source, route or draft' \
  'binding behind other conflicts.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: sonnet:high -->

### Task 4: Capture tool text says retraction is owner-only

**Files:**
- Modify: `packages/app/src/open_brain/services/local_mcp.py:349-356` (`brain_capture`), `:380-388` (`brain_capture_submit`)
- Modify: `packages/app/src/open_brain/services/local_entrypoints.py:1036-1041` (`mcp` parser description)
- Test: `packages/app/tests/integration/services/test_local_mcp.py`, `packages/app/tests/integration/services/test_local_entrypoints.py`

**Interfaces:**
- Consumes: nothing. Produces: wording only. No test pins the old "cannot selectively delete" text (checked with `rg`).

- [ ] **Step 1: Write the failing tests**

`test_local_mcp.py`:

```python
def test_capture_tool_text_names_owner_only_retraction() -> None:
    sink = cast(Any, lambda *_args, **_kwargs: {})
    adapter = LocalMcpAdapter(capture=sink, capture_submit=sink)
    descriptions = {tool["name"]: cast(str, tool["description"]) for tool in adapter.list_tools()}
    for name in ("brain_capture", "brain_capture_submit"):
        text = descriptions[name]
        assert "cannot selectively delete" not in text
        assert "owner-only" in text and "open-brain retract" in text
```

`test_local_entrypoints.py`:

```python
def test_mcp_help_names_owner_only_retraction(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(("mcp", "--help"), environment={}) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "cannot selectively delete" not in output
    assert "owner-only" in output and "open-brain retract" in output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/services/test_local_mcp.py::test_capture_tool_text_names_owner_only_retraction packages/app/tests/integration/services/test_local_entrypoints.py::test_mcp_help_names_owner_only_retraction`
Expected: FAIL on `"cannot selectively delete" not in text`.

- [ ] **Step 3: Rewrite the three strings**

`brain_capture` description:

```python
                    "description": (
                        "Store durable unverified text. Agents cannot remove captures; "
                        "retraction is an owner-only CLI operation (open-brain retract, "
                        "see docs/owner-retraction.md). "
                        "Privacy tier selection is owner-only and is refused here "
                        "with tier_not_permitted. "
                        "No publication, actions, or connector authority."
                    ),
```

`brain_capture_submit` description:

```python
                    "description": (
                        "Submit one destination-bound capture under the trusted startup "
                        "policy selected at launch. The requested privacy tier must be "
                        "inside that policy's allowed capture tiers; a missing tier "
                        "becomes unknown. Captures are durable unverified content; agents "
                        "cannot remove them. Retraction is an owner-only CLI operation "
                        "(open-brain retract, see docs/owner-retraction.md). No publication, "
                        "actions, or connector authority."
                    ),
```

`mcp` parser description: replace the fragment `"content; version " "0.1.0 cannot selectively delete unwanted captures. Stopping prevents further work "` with:

```python
            "content; agents cannot remove captures. Retraction is an owner-only CLI "
            "operation (open-brain retract). Stopping prevents further work "
```

Keep the rest of the description string unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/services/test_local_mcp.py packages/app/tests/integration/services/test_local_entrypoints.py -k "capture or mcp_help or help"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/app/src/open_brain/services/local_mcp.py \
  packages/app/src/open_brain/services/local_entrypoints.py \
  packages/app/tests/integration/services/test_local_mcp.py \
  packages/app/tests/integration/services/test_local_entrypoints.py
printf '%s\n' 'docs(mcp): say capture retraction is owner-only' '' \
  'Capture tool text no longer suggests agents might remove records; it points' \
  'to the owner-only open-brain retract CLI.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: sonnet:high -->

### Task 5: Docs — stale proposals, workflow-to-grant table, dev checkout, coordinates

**Files:**
- Modify: `docs/review-publication.md` (paragraph after the decision commands; Slack patch paragraph; Configure section)
- Modify: `docs/first-use.md:3-4`
- Modify: `docs/install.md` (`## Build from source`, after the first `sh` block)
- Modify: `README.md` (`## Develop`, after the `uv sync` block)

**Interfaces:** docs only. Grant names verified against `local_mcp.py:108-132` (`MCP_REGISTERED_TOOLS`) and `agent_setup.py:1209-1235`; setup flags verified against `local_entrypoints.py` (`--allow-search`, `--allow-content-read`, `--allow-history-read`, `--allow-review-read`, `--allow-review-propose`, `--allow-review-decide`). Coordinates verified against `docs/core-v01-features.md#compatibility-coordinates` (schema 23, runtime 16, Portable 18).

- [ ] **Step 1: Replace the conflict paragraph in `docs/review-publication.md`**

Replace the paragraph starting "A stale or wrong review token returns `review_conflict`." with:

```markdown
A stale or wrong review token returns `review_conflict`. The same code is returned when a selected
source or its route changed after inspection. The CLI names the changed binding (`source`,
`route`, or `draft`), and `--json` output carries it as `error.binding`. Inspect the proposal and
current state again before choosing a new action. A proposal can receive only one terminal
decision.

### Stale update proposals

An update proposal is bound to the page revision it was drafted against. If another approval
changes that page first, `review show` and `review list` report `stale: true`. The flag is derived
when read; nothing new is stored. Approve and edit-and-approve then return `review_target_stale`:
the target page changed after the proposal was drafted, so the proposal can only be rejected.
Rejection writes no page and skips the live source, route, and target checks, so a stale proposal
can always be cleared:

    open-brain review reject PROPOSAL_ID --review-token REVIEW_TOKEN

To carry the change forward, read the current page and propose a new update.
```

(Use a fenced `sh` block for the reject command, matching the rest of the page.)

- [ ] **Step 2: Fix the Slack patch sentence**

Replace "If the page changed, approval returns `review_conflict`; it never replaces the whole page." with:

```markdown
If another approval changed the page, approval returns `review_target_stale`; a manual edit to the
page returns `review_conflict`. Neither replaces the whole page.
```

- [ ] **Step 3: Add the workflow table**

After the paragraph "Capture, search, inbox-read, and organize grants do not enable review or publication. Review grants also do not enable those other capabilities." insert:

```markdown
### Workflows, grants, and restarts

| Workflow | MCP tools | Setup flags |
|---|---|---|
| Find a page and its current revision ID | `brain_search_page`, or `brain_history_list` | `--allow-search`, or `--allow-history-read` |
| Read a complete page or source | `brain_read` (needs `record_id` and a current `expected_revision_id`) | `--allow-content-read` |
| Read retained revisions | `brain_history_list`, `brain_history_show` | `--allow-history-read` |
| Inspect proposals | `brain_review_list`, `brain_review_show` | `--allow-review-read` |
| Draft a note or an update | `brain_review_propose` | `--allow-review-propose` |
| Approve, reject, or edit-and-approve | `brain_review_approve`, `brain_review_reject`, `brain_review_edit_and_approve` | `--allow-review-decide` |
| Merge new material into an existing page | all of the read and review tools above | `--allow-search` or `--allow-history-read`, plus `--allow-content-read`, `--allow-review-read`, `--allow-review-propose`, `--allow-review-decide` |

The MCP server reads its grants when the client starts it. After `agent setup ... --apply` adds a
grant, restart the agent client (or start a new session) before the new tools appear.
```

- [ ] **Step 4: First-use coordinates**

In `docs/first-use.md`, replace "local state schema **17**, runtime session **12**, and Portable\nmetadata **12**." with "local state schema **23**, runtime session **16**, and Portable\nmetadata **18**." (keep the line break position).

- [ ] **Step 5: Dev-checkout note**

`docs/install.md`, directly after the `sh` block in `## Build from source` that ends with `uv run --frozen --python 3.14 open-brain --help`:

```markdown
A source checkout does not put `open-brain` on your `PATH`. Run it through uv from the repository
root, for example `uv run --frozen open-brain status --json`.
```

`README.md`, directly after the `sh` block in `## Develop` containing `make contributor-check`:

```markdown
A checkout does not install `open-brain` on `PATH`; run it from the repository root as
`uv run --frozen open-brain ...`.
```

- [ ] **Step 6: Check the docs tests**

Run: `uv run --frozen pytest -q tests/release/test_documentation_examples.py packages/app/tests -k "documentation or contributor or readme"`
Expected: pass. `first-use.md` example hashes cover fenced examples only, and line 3 is prose.

- [ ] **Step 7: Commit**

```bash
git add docs/review-publication.md docs/first-use.md docs/install.md README.md
printf '%s\n' 'docs(review): stale proposals, grant table, dev checkout note' '' \
  'Document review_target_stale and the stale flag, map review workflows to' \
  'grants and the restart they need, note uv run for checkouts, and correct' \
  'the first-use coordinates to 23/16/18.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: sonnet:high -->

### Task 6: Phase A gates

**Files:** none modified unless a gate fails.

- [ ] **Step 1: Full verification**

Run: `make verify`
Expected: Ruff `All checks passed!`, MyPy `Success: no issues found`, pytest summary with no failures, successful builds, plugin tests pass. If a pre-existing test fails because it asserted `review_conflict` for a target that advanced through another approval, update that assertion to `review_target_stale` in a separate commit and say so in the commit message.

- [ ] **Step 2: Native audit (budget check)**

Run: `make native-audit`
Expected: pass. Reason: BUILD-021 records a tight marshal object budget; this phase adds engine and app code. If it fails with `artifact-invalid`, measure as BUILD-021 describes and stop; raising `MAX_OBJECTS` needs its own reviewed commit.

- [ ] **Step 3: Whitespace and workflow lint**

Run: `git diff --check origin/main...HEAD && actionlint .github/workflows/ci.yml`
Expected: no output, exit 0.

- [ ] **Step 4: Confirm coordinates did not move**

Run: `git diff origin/main...HEAD --stat -- packages/engine/src/open_brain_engine/engine/local_schema*.py packages/engine/src/open_brain_engine/engine/*migration*.py packages/engine/src/open_brain_engine/portable`
Expected: empty output (schema 23, runtime 16 and Portable 18 untouched).

---

# Phase B — PR B on `fix/review-friction-b` (items 2, 3, 5)

- [ ] **Create the branch from the tip of Phase A**

```bash
cd "$(git rev-parse --show-toplevel)"   # the review-friction worktree
git switch -c fix/review-friction-b
git log --oneline -1   # expect the Task 5 docs commit (or the last Phase A fix)
```

<!-- model: opus -->

### Task 7: `projection_applied` on `record.read` and `history.show`

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/contracts.py:632-636` (helper after the marker constants)
- Modify: `packages/engine/src/open_brain_engine/engine/records.py:19`, `:35-38`, `:210`, `:438`
- Modify: `packages/engine/src/open_brain_engine/engine/paging.py:872-881`
- Modify: `packages/engine/src/open_brain_engine/engine/t03_schema.json` (`record.read.response` ~297-345, `history.show.response` ~496-544)
- Modify (copy): `tests/fixtures/new-user-t03/strict-contract.schema.json`
- Modify: `packages/obsidian-plugin/src/t03-wire.ts:77-82`
- Modify: `docs/records-and-history.md`
- Create: `packages/app/tests/integration/engine/test_record_read_projection.py`
- Test: `packages/engine/tests/test_t03_values.py`, `packages/obsidian-plugin/tests/t03-wire.test.ts`

**Interfaces:**
- Produces: `contracts.PUBLIC_PROJECTION_MARKERS: tuple[str, str, str]` = `("[protected]", "[private-path]", "[redacted]")` (exact strings from `_PUBLIC_LITERAL_MARKER`, `_PUBLIC_PATH_MARKER`, `_PUBLIC_CREDENTIAL_MARKER`). `contracts.projection_markers_added(original: str, candidate: str) -> bool` is `True` when `candidate` holds more of any marker than `original`. Task 8 reuses it.
- Produces: `records.ProjectedRecord.projection_applied: bool = False`. Wire field `projection_applied: boolean`, optional in the schema and always emitted by the current engine for `record.read` and `history.show`.
- Pin note: the schema change is pinned only by byte identity between `t03_schema.json` and `strict-contract.schema.json`. This task updates both. `t06-client-api.json`'s `record.read` example stays valid because the field is optional.

- [ ] **Step 1: Write the failing tests**

Create `packages/app/tests/integration/engine/test_record_read_projection.py`:

```python
"""Record reads disclose when public projection replaced stored text."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest
from open_brain_engine.portable.v4 import canonical_revision_id

from open_brain.profile import compile_single_user_local

OWNER = EffectiveAuthority(
    "projection-owner", "projection-session", frozenset(), None, owner=True
)


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=("Notes",)))


def _wire(response: object) -> dict[str, Any]:
    return cast(dict[str, Any], cast(Any, response).to_wire())


def test_source_read_flags_projected_text(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space_id = engine.inbox.spaces()[0].space_id
    plain, pathy = (
        engine.capture.accept(
            TextPayload(text), delivery_id=f"projection.{index}", space_id=space_id
        ).capture_id
        for index, text in enumerate(
            ("Plain synthetic note", "Tool at /srv/synthetic/tool.py")
        )
    )

    clean = _wire(
        engine.retrieval.read_record(
            RecordReadRequest(record_id=plain, expected_revision_id=plain), authority=OWNER
        )
    )
    flagged = _wire(
        engine.retrieval.read_record(
            RecordReadRequest(record_id=pathy, expected_revision_id=pathy), authority=OWNER
        )
    )

    assert clean["projection_applied"] is False
    assert flagged["projection_applied"] is True
    assert "[private-path]" in flagged["content"]["text"]
    assert "/srv/synthetic" not in flagged["content"]["text"]


def test_canonical_read_and_history_show_flag_projected_text(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space_id = engine.inbox.spaces()[0].space_id
    source = engine.capture.accept(
        TextPayload("Synthetic canonical source"),
        delivery_id="projection.canonical.source",
        space_id=space_id,
    ).capture_id
    proposal = engine.review.propose(
        (source,),
        (ProposalDraft("Projected page", "Tool at /srv/synthetic/tool.py"),),
        delivery_id="projection.canonical",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="projection.canonical.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert decision.page_id is not None and decision.publication_id is not None
    revision = canonical_revision_id(decision.publication_id)
    request = RecordReadRequest(record_id=decision.page_id, expected_revision_id=revision)

    assert _wire(engine.retrieval.read_record(request, authority=OWNER))[
        "projection_applied"
    ] is True
    assert _wire(engine.history.read_history(request, authority=OWNER))[
        "projection_applied"
    ] is True
```

Append to `packages/engine/tests/test_t03_values.py`:

```python
def test_read_responses_accept_an_optional_boolean_projection_flag() -> None:
    baseline = next(
        cast(dict[str, Any], parse_wire(str(case["raw"])))
        for case in CASES
        if case["schema"] == "record.read.response" and case["accept"]
    )
    for schema in ("record.read.response", "history.show.response"):
        validate_wire(schema, baseline)
        validate_wire(schema, {**baseline, "projection_applied": True})
        with pytest.raises(T03Error):
            validate_wire(schema, {**baseline, "projection_applied": "yes"})
```

Append to `packages/obsidian-plugin/tests/t03-wire.test.ts`:

```ts
describe("record read projection flag", () => {
  const original = JSON.parse(
    corpus.cases.find(item => item.accept && item.schema === "record.read.response")!.raw,
  ) as Record<string, unknown>;

  it("accepts an optional boolean projection_applied", () => {
    const flagged = { ...original, projection_applied: true };
    expect(validateT03Wire("record.read.response", flagged)).toBe(flagged);
    expect(validateT03Wire("record.read.response", original)).toBe(original);
  });

  it("rejects a non-boolean projection_applied", () => {
    expect(() => validateT03Wire("record.read.response", { ...original, projection_applied: "yes" })).toThrow();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_record_read_projection.py packages/engine/tests/test_t03_values.py::test_read_responses_accept_an_optional_boolean_projection_flag`
Expected: FAIL with `KeyError: 'projection_applied'` and `T03Error` on the flagged value.

Run: `npm --prefix packages/obsidian-plugin ci --ignore-scripts --no-audit --no-fund && npm --prefix packages/obsidian-plugin test -- tests/t03-wire.test.ts`
Expected: FAIL on "accepts an optional boolean projection_applied".

- [ ] **Step 3: Marker helper in `contracts.py`**

Insert after `_PUBLIC_OUTPUT_DECODING_PASSES = 4`:

```python
PUBLIC_PROJECTION_MARKERS = (
    _PUBLIC_LITERAL_MARKER,
    _PUBLIC_PATH_MARKER,
    _PUBLIC_CREDENTIAL_MARKER,
)


def projection_markers_added(original: str, candidate: str) -> bool:
    """Return whether candidate holds more public projection placeholders than original."""
    return any(
        candidate.count(marker) > original.count(marker) for marker in PUBLIC_PROJECTION_MARKERS
    )
```

Marker counting is used instead of `projected != raw` because harness memories are re-encoded and text is NFC-normalized during projection; only the placeholders mean content was withheld.

- [ ] **Step 4: Carry the flag through `records.py`**

Change the import on line 19 to `from .contracts import LocalEngineContext, projection_markers_added`.

```python
@dataclass(frozen=True, slots=True)
class ProjectedRecord:
    summary: dict[str, Any]
    text: str
    indexed_text: str | None = None
    projection_applied: bool = False
```

At the end of `source()` replace `return ProjectedRecord(summary, body, indexed_text=indexed_body)` with:

```python
        return ProjectedRecord(
            summary,
            body,
            indexed_text=indexed_body,
            projection_applied=(
                projection_markers_added(text, body)
                or projection_markers_added(title, cast(str, summary["title"]))
            ),
        )
```

At the end of `canonical()` replace `return ProjectedRecord(summary, body, indexed_text)` with:

```python
        return ProjectedRecord(
            summary,
            body,
            indexed_text,
            projection_applied=(
                projection_markers_added(page.body, body)
                or projection_markers_added(cast(str, page.fields["title"]), title)
            ),
        )
```

Confirm `cast` is already imported in `records.py`; add it to the `typing` import if not.

- [ ] **Step 5: Emit the field in `paging.read_record`**

In the `response = {...}` literal add after `"next_cursor": None,`:

```python
            "projection_applied": projected.projection_applied,
```

The flag describes the whole record, so continuation chunks repeat it.

- [ ] **Step 6: Schema, both copies**

In `packages/engine/src/open_brain_engine/engine/t03_schema.json`, in both `"record.read.response"` and `"history.show.response"`, change the tail from:

```json
        "end_byte": {
          "type": "integer",
          "min": 0,
          "max": 9007199254740991
        }
      },
      "optional": []
    },
```

to:

```json
        "end_byte": {
          "type": "integer",
          "min": 0,
          "max": 9007199254740991
        },
        "projection_applied": {
          "type": "boolean"
        }
      },
      "optional": [
        "projection_applied"
      ]
    },
```

Then copy it byte-for-byte to the frozen fixture and confirm:

```bash
cp packages/engine/src/open_brain_engine/engine/t03_schema.json \
  tests/fixtures/new-user-t03/strict-contract.schema.json
cmp packages/engine/src/open_brain_engine/engine/t03_schema.json \
  tests/fixtures/new-user-t03/strict-contract.schema.json && echo IDENTICAL
```

Expected: `IDENTICAL`.

- [ ] **Step 7: Plugin wire grammar**

In `packages/obsidian-plugin/src/t03-wire.ts`, change the `record.read.response` rule head:

```ts
  "record.read.response": { type: "object", optional: ["selection", "projection_applied"], properties: {
    selection, projection_applied: { type: "boolean" },
```

Leave the remaining properties as they are.

- [ ] **Step 8: Document it**

In `docs/records-and-history.md`, after the sentence ending "a continuation cursor and a completion flag." insert:

```markdown
`projection_applied` is `true` when protected values in the record were replaced with
`[protected]`, `[redacted]`, or `[private-path]` in this response. The stored record keeps the
original text. Do not copy a placeholder into an edit; see
[review and publish](review-publication.md).
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_record_read_projection.py packages/engine/tests/test_t03_values.py packages/app/tests/unit/test_t03_contracts.py packages/app/tests/unit/test_t03_adapters.py packages/app/tests/unit/test_plugin_bridge.py`
Expected: all pass.

Run: `npm --prefix packages/obsidian-plugin test -- tests/t03-wire.test.ts tests/t03-contracts.test.ts`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/contracts.py \
  packages/engine/src/open_brain_engine/engine/records.py \
  packages/engine/src/open_brain_engine/engine/paging.py \
  packages/engine/src/open_brain_engine/engine/t03_schema.json \
  tests/fixtures/new-user-t03/strict-contract.schema.json \
  packages/obsidian-plugin/src/t03-wire.ts packages/obsidian-plugin/tests/t03-wire.test.ts \
  packages/engine/tests/test_t03_values.py \
  packages/app/tests/integration/engine/test_record_read_projection.py \
  docs/records-and-history.md
printf '%s\n' 'feat(read): disclose projection on record reads' '' \
  'brain_read and history show now return projection_applied when protected' \
  'values were replaced. The field is optional in the T03 schema; the frozen' \
  'fixture copy is updated with it.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: opus -->

### Task 8: Edit-and-approve refuses placeholders the stored page does not contain

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/review.py` (imports; new method before `_decide`; guard after the review-token check)
- Modify: `packages/app/src/open_brain/services/review_publication.py:348-382` (string map)
- Modify: `packages/app/src/open_brain/services/local_entrypoints.py` (`_write_review_failure` messages)
- Modify: `packages/app/src/open_brain/services/mcp_protocol.py` (allowlist)
- Modify: `packages/app/src/open_brain/services/plugin_bridge.py` (`_BRIDGE_REVIEW_CODES`)
- Modify: `docs/review-publication.md`
- Test: `packages/app/tests/integration/engine/test_review_publication_engine.py`, `packages/app/tests/integration/services/test_review_publication.py`, `packages/app/tests/integration/services/test_local_mcp.py`

**Interfaces:**
- Consumes: `projection_markers_added` (Task 7); `_target_state`, `_read_page`, `parse_markdown` already imported in `review.py`.
- Produces: engine `ValueError("replacement contains projection placeholder")`; public code `replacement_contains_redaction`. Baseline: the live target page body for update and patch proposals, the stored raw draft body for create proposals.

- [ ] **Step 1: Write the failing tests**

Engine, append to `test_review_publication_engine.py`:

```python
def test_edit_refuses_projection_placeholders_missing_from_the_live_page(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    created = engine.review.propose(
        (first,),
        (ProposalDraft("Guarded", "Code lives at /srv/synthetic/tool.py\n"),),
        delivery_id="delivery.guard.create",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.guard.create.approve",
        expected_review_digest=created.review_digest,
    )
    update = engine.review.propose(
        (second,),
        (ProposalDraft("Guarded", "Code lives at /srv/synthetic/tool.py\nMore\n"),),
        delivery_id="delivery.guard.update",
        target_page_id=created.page_id,
    )[0]
    shown = engine.review.show(update.proposal_id)
    assert "[private-path]" in shown.markdown

    with pytest.raises(ValueError, match="replacement contains projection placeholder"):
        engine.review.decide(
            update.proposal_id,
            DecisionOutcome.EDITED,
            delivery_id="delivery.guard.edit.projected",
            edited_markdown=shown.markdown + "\nMerged",
            expected_review_digest=update.review_digest,
        )
    assert engine.review.show(update.proposal_id).status == "pending"
    restored = engine.review.decide(
        update.proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.guard.edit.restored",
        edited_markdown="Code lives at /srv/synthetic/tool.py\nMerged\n",
        expected_review_digest=update.review_digest,
    )
    assert restored.outcome is DecisionOutcome.EDITED


def test_create_edit_allows_only_placeholders_already_in_the_draft(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,),
        (ProposalDraft("Literal marker", "Draft keeps a literal [redacted] marker"),),
        delivery_id="delivery.guard.literal",
    )[0]
    with pytest.raises(ValueError, match="replacement contains projection placeholder"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.EDITED,
            delivery_id="delivery.guard.literal.added",
            edited_markdown="Draft keeps a literal [redacted] marker and [protected] text",
            expected_review_digest=proposal.review_digest,
        )
    kept = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.guard.literal.kept",
        edited_markdown="Edited draft keeps a literal [redacted] marker",
        expected_review_digest=proposal.review_digest,
    )
    assert kept.outcome is DecisionOutcome.EDITED
```

Service, append to `test_review_publication.py`:

```python
def test_edit_with_placeholders_maps_to_replacement_contains_redaction() -> None:
    service, task = _service()
    task.decide.side_effect = ValueError("replacement contains projection placeholder")
    with pytest.raises(ReviewPublicationError, match="^replacement_contains_redaction$"):
        service.edit_and_approve(
            {"proposal_id": PROPOSAL, "review_token": DIGEST, "markdown": "[protected]"}
        )
```

MCP: extend the parametrize list of `test_review_safe_errors_survive_mcp_transport` to `["review_target_stale", "replacement_contains_redaction"]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py -k "placeholder" packages/app/tests/integration/services/test_review_publication.py::test_edit_with_placeholders_maps_to_replacement_contains_redaction packages/app/tests/integration/services/test_local_mcp.py::test_review_safe_errors_survive_mcp_transport`
Expected: FAIL (`DID NOT RAISE`, `operation_failed`, `tool call failed`).

- [ ] **Step 3: Engine guard in `review.py`**

Add `projection_markers_added` to the `from .contracts import (...)` block. Add this method directly before `_decide`:

```python
    def _edit_baseline_body(
        self,
        proposal_id: str,
        context: tuple[dict[str, object], dict[str, object]] | None,
    ) -> str:
        """Return the stored body an edit replaces: the live target, else the raw draft."""
        connection = self._store.connect()
        try:
            if context is not None and context[1]["operation"] == "update":
                target = _target_state(self, connection, cast(str, context[1]["page_id"]))
                assert target is not None
                return parse_markdown(_read_page(self, cast(str, target["canonical_path"]))).body
            proposal = connection.execute(
                "SELECT proposed_kind, proposed_bytes, body FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        finally:
            connection.close()
        if proposal is None:
            raise ValueError("unknown proposal")
        if cast(str, proposal["proposed_kind"]) == "page_update":
            return parse_markdown(cast(bytes, proposal["proposed_bytes"])).body
        return cast(str | None, proposal["body"]) or ""
```

In `_decide`, directly after the `raise ReviewBindingConflict("review digest conflict", binding="draft")` block and before the `validate_current_binding` block:

```python
        if (
            outcome is DecisionOutcome.EDITED
            and reserved is None
            and projection_markers_added(
                self._edit_baseline_body(proposal_id, context), cast(str, edited_markdown)
            )
        ):
            raise ValueError("replacement contains projection placeholder")
```

`reserved is None` skips the guard on idempotent replays of a recorded decision.

- [ ] **Step 4: Public code, CLI text, MCP and bridge**

`review_publication.py` string map, add:

```python
        "replacement contains projection placeholder": "replacement_contains_redaction",
```

`local_entrypoints.py` `_write_review_failure` messages, add:

```python
        "replacement_contains_redaction": (
            "The replacement body contains a privacy placeholder ([protected], [redacted], or "
            "[private-path]) that the current page does not. Restore the original text first."
        ),
```

`mcp_protocol.py`: add `"replacement_contains_redaction",` after `"review_target_stale",`.

`plugin_bridge.py`:

```python
_BRIDGE_REVIEW_CODES = {
    "review_target_stale": "review_conflict",
    "replacement_contains_redaction": "review_conflict",
}
```

- [ ] **Step 5: Document it**

In `docs/review-publication.md`, after "Edit-and-approve replaces the body supplied by the proposal while retaining the same validated sources and destination." add:

```markdown
Edit-and-approve refuses with `replacement_contains_redaction` when the replacement holds more
`[protected]`, `[redacted]`, or `[private-path]` placeholders than the stored text it replaces (the
current page for an update, the stored draft for a new note). Those placeholders come from privacy
projection in `review show` and `brain_read`; copying them back would overwrite real content.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/engine/test_review_patches.py packages/app/tests/integration/services/test_review_publication.py packages/app/tests/integration/services/test_local_mcp.py packages/app/tests/unit/test_plugin_bridge.py`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/review.py \
  packages/app/src/open_brain/services/review_publication.py \
  packages/app/src/open_brain/services/local_entrypoints.py \
  packages/app/src/open_brain/services/mcp_protocol.py \
  packages/app/src/open_brain/services/plugin_bridge.py \
  packages/app/tests/integration/engine/test_review_publication_engine.py \
  packages/app/tests/integration/services/test_review_publication.py \
  packages/app/tests/integration/services/test_local_mcp.py docs/review-publication.md
printf '%s\n' 'fix(review): refuse edits that write privacy placeholders back' '' \
  'edit-and-approve returns replacement_contains_redaction when the replacement' \
  'adds [protected], [redacted] or [private-path] markers the stored text lacks.' '' \
  'Refs #126' > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: opus -->

### Task 9: Full-page update proposals show `replacement` and a projected `page_diff`

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/contracts.py` (`ReviewProposal`)
- Modify: `packages/engine/src/open_brain_engine/engine/review.py` (module helper before `class ReviewOperations`; `_show_proposal`)
- Modify: `packages/app/src/open_brain/services/review_publication.py` (`show`)
- Modify: `packages/app/src/open_brain/services/local_entrypoints.py` (`_run_review` show branch)
- Modify: `packages/app/src/open_brain/services/plugin_bridge.py` (`_BRIDGE_HIDDEN_REVIEW_FIELDS`)
- Modify: `docs/review-publication.md`
- Test: `packages/app/tests/integration/engine/test_review_publication_engine.py`, `packages/app/tests/integration/engine/test_review_patches.py`, `packages/app/tests/integration/services/test_review_publication.py`, `packages/app/tests/unit/test_plugin_bridge.py`

**Interfaces:**
- Consumes: `stale` (Task 2), `MAX_REVIEW_MARKDOWN_BYTES` (already imported in `review.py`).
- Produces: `ReviewProposal.replacement: str | None = None` (`"full_page"` only for a bound `update` without a typed patch) and `ReviewProposal.page_diff: str | None = None` (unified diff, `fromfile="current"`, `tofile="proposed"`, of the projected live body against the projected draft; `None` when the diff exceeds 64 KiB of UTF-8 or the live page cannot be read). `service.show` omits both keys when `replacement` is `None`, so create and patch output is unchanged.

- [ ] **Step 1: Write the failing tests**

Engine, append to `test_review_publication_engine.py`:

```python
def test_full_page_update_shows_replacement_and_bounded_diff(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    third = _text_sources(engine, 1, prefix="diff")[0]
    created = engine.review.propose(
        (first,),
        (ProposalDraft("Diffed", "Keep this line\nTool at /srv/synthetic/tool.py\n"),),
        delivery_id="delivery.diff.create",
    )[0]
    assert engine.review.show(created.proposal_id).replacement is None
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.diff.create.approve",
        expected_review_digest=created.review_digest,
    )
    update = engine.review.propose(
        (second,),
        (ProposalDraft("Diffed", "Replacement only\n"),),
        delivery_id="delivery.diff.update",
        target_page_id=created.page_id,
    )[0]
    shown = engine.review.show(update.proposal_id)
    assert shown.replacement == "full_page"
    diff = shown.page_diff or ""
    assert "-Keep this line" in diff and "+Replacement only" in diff
    assert "/srv/synthetic" not in diff and "[private-path]" in diff
    assert shown.projection_applied is True

    big_page = engine.review.propose(
        (third,),
        (ProposalDraft("Big page", "a\n" * 20_000),),
        delivery_id="delivery.diff.big.create",
    )[0]
    engine.review.decide(
        big_page.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.diff.big.approve",
        expected_review_digest=big_page.review_digest,
    )
    rewrite = engine.review.propose(
        (third,),
        (ProposalDraft("Big page", "b\n" * 20_000),),
        delivery_id="delivery.diff.big.update",
        target_page_id=big_page.page_id,
    )[0]
    omitted = engine.review.show(rewrite.proposal_id)
    assert omitted.replacement == "full_page"
    assert omitted.page_diff is None
```

The 40,000-byte bodies stay under the 64 KiB draft limit, while their diff (about 120,000 bytes) exceeds the diff bound.

Patch, in `test_review_patches.py::test_patch_is_bound_shown_as_diff_and_applied_with_idempotent_retries`, after `assert "+Slack update" in (shown.patch_diff or "")` add:

```python
    assert shown.replacement is None and shown.page_diff is None
```

Service, append to `test_review_publication.py`:

```python
def test_show_omits_replacement_keys_unless_full_page_update() -> None:
    service, task = _service()
    task.show.return_value = _detail()
    assert "replacement" not in service.show({"proposal_id": PROPOSAL})
    task.show.return_value = replace(
        _detail(), operation="update", replacement="full_page", page_diff=None
    )
    shown = service.show({"proposal_id": PROPOSAL})
    assert shown["replacement"] == "full_page" and shown["page_diff"] is None
```

Bridge, append to `test_plugin_bridge.py`:

```python
def test_bridge_hides_full_page_diff_fields() -> None:
    hidden = plugin_bridge_module._frozen_publication_result(
        {"status": "shown", "replacement": "full_page", "page_diff": "-a\n+b\n"}
    )
    assert hidden == {"status": "shown"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py::test_full_page_update_shows_replacement_and_bounded_diff packages/app/tests/integration/engine/test_review_patches.py packages/app/tests/integration/services/test_review_publication.py::test_show_omits_replacement_keys_unless_full_page_update packages/app/tests/unit/test_plugin_bridge.py::test_bridge_hides_full_page_diff_fields`
Expected: FAIL (`AttributeError: ... 'replacement'`).

- [ ] **Step 3: DTO fields**

In `contracts.py`, append to `ReviewProposal` after `stale: bool = False`:

```python
    replacement: str | None = None
    page_diff: str | None = None
```

- [ ] **Step 4: Diff helper in `review.py`**

Insert between the `if TYPE_CHECKING:` block and `class ReviewOperations`:

```python
def _bounded_page_diff(current: str, proposed: str) -> str | None:
    """Return a unified diff of projected bodies, or None past the inspection limit."""
    diff = "".join(
        difflib.unified_diff(
            (current + "\n").splitlines(keepends=True),
            (proposed + "\n").splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
        )
    )
    return diff if len(diff.encode("utf-8")) <= MAX_REVIEW_MARKDOWN_BYTES else None
```

- [ ] **Step 5: Read and project the live body in `_show_proposal`**

Inside the `try:` block, after the `stale = (...)` assignment from Task 2:

```python
            live_body: str | None = None
            if (
                binding is not None
                and binding["operation"] == "update"
                and binding.get("patch") is None
            ):
                try:
                    target = _target_state(self, connection, cast(str, binding["page_id"]))
                    assert target is not None
                    live_body = parse_markdown(
                        _read_page(self, cast(str, target["canonical_path"]))
                    ).body.removesuffix("\n")
                except ValueError:
                    live_body = None
```

After the existing `patch_diff` block and before `return ReviewProposal(`:

```python
        replacement: str | None = None
        page_diff: str | None = None
        live_projected = False
        if binding is not None and operation == "update" and patch is None:
            replacement = "full_page"
            if live_body is not None:
                projected_live = project_public_result_text(
                    live_body, protected_literals=references
                )
                live_projected = projected_live != live_body
                page_diff = _bounded_page_diff(projected_live, markdown)
```

In the constructor, change `projection_applied=(...)` to:

```python
            projection_applied=(
                markdown != raw_markdown
                or title != raw_title
                or evidence_projected
                or live_projected
            ),
```

and add after `stale=stale,`:

```python
            replacement=replacement,
            page_diff=page_diff,
```

Both sides of the diff pass through `project_public_result_text` with the same `references` as the draft, so the diff never shows a value the draft view would hide.

- [ ] **Step 6: Service, CLI and bridge**

`review_publication.py` `show`, after the `if proposal.patch is None:` block:

```python
        if proposal.replacement is None:
            result.pop("replacement")
            result.pop("page_diff")
```

`local_entrypoints.py` `_run_review` show branch, after the stale line from Task 3 and before the patch block:

```python
        if result.get("replacement") == "full_page":
            print("Approving replaces the entire current page body.")
            if result.get("page_diff") is not None:
                print(_terminal_text("Diff against the current page:"))
                print(_terminal_text(cast(str, result["page_diff"])))
            else:
                print("Diff omitted: it exceeds 64 KiB or the current page could not be read.")
```

`plugin_bridge.py`:

```python
_BRIDGE_HIDDEN_REVIEW_FIELDS = frozenset({"stale", "replacement", "page_diff"})
```

- [ ] **Step 7: Document it**

In `docs/review-publication.md`, in "Update an existing canonical note", after "Open Brain rejects a stale or manually changed target instead of overwriting it." add:

```markdown
An update proposal replaces the whole page body. `review show` labels it with
`replacement: "full_page"` and includes `page_diff`, a unified diff from the current page to the
proposed body, projected the same way as the draft. `page_diff` is `null` when the diff would exceed
64 KiB or the current page cannot be read; the draft itself is still complete. Typed Slack patches
keep their own `patch_diff` and are not labelled as replacements.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/engine/test_review_patches.py packages/app/tests/integration/services/test_review_publication.py packages/app/tests/unit/test_plugin_bridge.py packages/app/tests/integration/services/test_local_entrypoints.py -k "review or bridge or patch or diff"`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/contracts.py \
  packages/engine/src/open_brain_engine/engine/review.py \
  packages/app/src/open_brain/services/review_publication.py \
  packages/app/src/open_brain/services/local_entrypoints.py \
  packages/app/src/open_brain/services/plugin_bridge.py \
  packages/app/tests/integration/engine/test_review_publication_engine.py \
  packages/app/tests/integration/engine/test_review_patches.py \
  packages/app/tests/integration/services/test_review_publication.py \
  packages/app/tests/unit/test_plugin_bridge.py docs/review-publication.md
printf '%s\n' 'feat(review): label full-page updates and show a projected diff' '' \
  'review show marks update proposals as full-page replacements and includes a' \
  'bounded unified diff against the current page.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: opus -->

### Task 10: Refuse duplicate creates at decision time, with an explicit override

**Files:**
- Modify: `packages/engine/src/open_brain_engine/engine/review_bound.py` (imports; new function)
- Modify: `packages/engine/src/open_brain_engine/engine/review.py` (`_decide` signature and transaction branch; `ReviewTasks.decide`)
- Modify: `packages/engine/src/open_brain_engine/engine/contracts.py:3219-3227` (`ReviewTask.decide`)
- Modify: `packages/app/src/open_brain/services/review_publication.py` (`_OPTIONAL`, validation, `_decide`, string map)
- Modify: `packages/app/src/open_brain/services/local_mcp.py:630-668` (approve and edit tool schemas)
- Modify: `packages/app/src/open_brain/services/local_entrypoints.py:1645-1657` (parser), `_review_arguments`, `_write_review_failure`
- Modify: `packages/app/src/open_brain/services/mcp_protocol.py`, `packages/app/src/open_brain/services/plugin_bridge.py`
- Modify: `docs/review-publication.md`
- Test: `packages/app/tests/integration/engine/test_review_publication_engine.py`, `packages/app/tests/integration/services/test_review_publication.py`, `packages/app/tests/integration/services/test_local_mcp.py`, `packages/app/tests/integration/services/test_local_entrypoints.py`

**Interfaces:**
- Produces: `review_bound.title_key(title: str) -> str` (NFC, casefold, whitespace runs collapsed to one space, trimmed). `review_bound.duplicate_page_id(connection, *, space_id: str, page_id: str, title: str, capture_ids: Sequence[str], sibling_page_ids: frozenset[str]) -> str | None`. Engine `ValueError("duplicate canonical page")`; public code `duplicate_page`.
- Produces: `ReviewTask.decide(..., allow_duplicate: bool = False)`, `ReviewTasks.decide(..., allow_duplicate: bool = False)`, `ReviewOperations._decide(..., allow_duplicate: bool = False)`. MCP argument `allow_duplicate: boolean` on `brain_review_approve` and `brain_review_edit_and_approve` only. CLI flag `--allow-duplicate` on `review approve` and `review edit-and-approve`.
- `allow_duplicate` is not part of the decision request hash, so the stored `request_sha256` format is unchanged; a replay of a recorded decision returns it whatever the flag.

- [ ] **Step 1: Write the failing tests**

Engine, append to `test_review_publication_engine.py`:

```python
def test_create_approval_refuses_a_duplicate_title_or_source_set(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    third = _text_sources(engine, 1, prefix="duplicate")[0]
    original, same_title, same_sources = (
        engine.review.propose(
            (source,), (ProposalDraft(title, body),), delivery_id=f"delivery.duplicate.{body}"
        )[0]
        for source, title, body in (
            (first, "Weekly  Notes", "Original"),
            (second, "weekly notes", "Same-title"),
            (first, "Different title", "Same-sources"),
        )
    )
    engine.review.decide(
        original.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.duplicate.original.approve",
        expected_review_digest=original.review_digest,
    )

    for proposal, outcome, edited in (
        (same_title, DecisionOutcome.APPROVED, None),
        (same_sources, DecisionOutcome.EDITED, "Edited same sources"),
    ):
        with pytest.raises(ValueError, match="duplicate canonical page"):
            engine.review.decide(
                proposal.proposal_id,
                outcome,
                delivery_id=f"delivery.duplicate.refused.{proposal.proposal_id}",
                edited_markdown=edited,
                expected_review_digest=proposal.review_digest,
            )
        assert engine.review.show(proposal.proposal_id).status == "pending"

    allowed = engine.review.decide(
        same_title.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.duplicate.allowed",
        expected_review_digest=same_title.review_digest,
        allow_duplicate=True,
    )
    assert allowed.page_id == same_title.page_id != original.page_id
    unrelated = engine.review.propose(
        (third,), (ProposalDraft("Unrelated", "Fresh"),), delivery_id="delivery.duplicate.fresh"
    )[0]
    assert engine.review.decide(
        unrelated.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.duplicate.fresh.approve",
        expected_review_digest=unrelated.review_digest,
    ).publication_id is not None


def test_sibling_drafts_from_one_source_are_not_duplicates(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, _ = _sources(engine)
    siblings = engine.review.propose(
        (first,),
        (ProposalDraft("Alpha", "Alpha body"), ProposalDraft("Beta", "Beta body")),
        delivery_id="delivery.siblings",
    )
    for index, proposal in enumerate(siblings):
        decided = engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"delivery.siblings.approve.{index}",
            expected_review_digest=proposal.review_digest,
        )
        assert decided.publication_id is not None
```

Service, append to `test_review_publication.py`:

```python
def test_allow_duplicate_is_a_boolean_override_on_publishing_decisions() -> None:
    service, task = _service()
    task.decide.return_value = DecisionRecord(
        decision_id="decision_20770d1b-e42a-4e77-b3de-bf127b36cfe6",
        proposal_id=PROPOSAL,
        outcome=DecisionOutcome.APPROVED,
        page_id=PAGE,
        publication_id="publication_a640a04f-801b-42e4-b09f-e7f57bb7ac22",
    )
    service.approve({"proposal_id": PROPOSAL, "review_token": DIGEST, "allow_duplicate": True})
    assert task.decide.call_args.kwargs["allow_duplicate"] is True
    service.approve({"proposal_id": PROPOSAL, "review_token": DIGEST})
    assert task.decide.call_args.kwargs["allow_duplicate"] is False
    validate_review_arguments(
        "edit_and_approve",
        {"proposal_id": PROPOSAL, "review_token": DIGEST, "markdown": "x", "allow_duplicate": False},
    )
    for operation, arguments in (
        ("approve", {"proposal_id": PROPOSAL, "review_token": DIGEST, "allow_duplicate": "true"}),
        ("reject", {"proposal_id": PROPOSAL, "review_token": DIGEST, "allow_duplicate": True}),
    ):
        with pytest.raises(ReviewPublicationError, match="^invalid_arguments$"):
            validate_review_arguments(operation, arguments)
    task.decide.side_effect = ValueError("duplicate canonical page")
    with pytest.raises(ReviewPublicationError, match="^duplicate_page$"):
        service.approve({"proposal_id": PROPOSAL, "review_token": DIGEST})
```

MCP, in `test_local_mcp.py`: extend the `test_review_safe_errors_survive_mcp_transport` parametrize list to `["review_target_stale", "replacement_contains_redaction", "duplicate_page"]`, and append:

```python
def test_only_publishing_decisions_accept_allow_duplicate() -> None:
    def operation(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"status": "ok"}

    adapter = LocalMcpAdapter(
        review_approve=operation, review_reject=operation, review_edit_and_approve=operation
    )
    properties = {
        tool["name"]: cast(dict[str, Any], tool["inputSchema"])["properties"]
        for tool in adapter.list_tools()
    }
    assert properties["brain_review_approve"]["allow_duplicate"] == {"type": "boolean"}
    assert properties["brain_review_edit_and_approve"]["allow_duplicate"] == {"type": "boolean"}
    assert "allow_duplicate" not in properties["brain_review_reject"]
```

CLI, append to `test_local_entrypoints.py`:

```python
def test_owner_review_cli_refuses_duplicate_create_unless_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform_name: str
) -> None:
    home = _private_home(tmp_path)
    brain = home / "brain"
    tasks = open_local_engine(compile_single_user_local(brain))
    space = tasks.spaces.create_space("Projects", delivery_id="review.cli.duplicate.space")
    first, second = (
        tasks.capture.accept(
            TextPayload(f"Duplicate CLI source {index}"),
            delivery_id=f"review.cli.duplicate.capture.{index}",
            space_id=space.space_id,
        ).capture_id
        for index in range(2)
    )
    original, repeat = (
        tasks.review.propose(
            (source,), (ProposalDraft("Duplicate CLI", body),), delivery_id=f"review.cli.dup.{body}"
        )[0]
        for source, body in ((first, "Original"), (second, "Repeat"))
    )
    tasks.review.decide(
        original.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="review.cli.duplicate.original",
        expected_review_digest=original.review_digest,
    )

    def cli(*arguments: str) -> int:
        return run_cli(
            (*arguments, "--data-dir", str(brain), "--json"),
            environment={"HOME": str(home)},
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )

    token = cast(str, repeat.review_digest)
    assert cli("review", "approve", repeat.proposal_id, "--review-token", token) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "duplicate_page"
    assert (
        cli("review", "approve", repeat.proposal_id, "--review-token", token, "--allow-duplicate")
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "approved"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py -k "duplicate or sibling" packages/app/tests/integration/services/test_review_publication.py::test_allow_duplicate_is_a_boolean_override_on_publishing_decisions packages/app/tests/integration/services/test_local_mcp.py -k "allow_duplicate or safe_errors" packages/app/tests/integration/services/test_local_entrypoints.py::test_owner_review_cli_refuses_duplicate_create_unless_allowed`
Expected: FAIL (`DID NOT RAISE`, `unexpected keyword argument 'allow_duplicate'`, `KeyError: 'allow_duplicate'`, CLI usage error exit 2).

- [ ] **Step 3: Duplicate lookup in `review_bound.py`**

Add `import unicodedata` to the stdlib imports. Add after `target_head_advanced`:

```python
def title_key(title: str) -> str:
    """Compare titles case-insensitively with whitespace runs collapsed."""
    return " ".join(unicodedata.normalize("NFC", title).casefold().split())


def duplicate_page_id(
    connection: sqlite3.Connection,
    *,
    space_id: str,
    page_id: str,
    title: str,
    capture_ids: Sequence[str],
    sibling_page_ids: frozenset[str],
) -> str | None:
    """Return an existing canonical page in the space that a new page would duplicate.

    A proposal whose page already exists is an update and is never a duplicate.
    Pages from sibling drafts of the same proposal set share sources by design,
    so only their titles are compared.
    """
    if (
        connection.execute(
            "SELECT 1 FROM search_documents WHERE result_id = ? AND record_type = 'canonical'",
            (page_id,),
        ).fetchone()
        is not None
    ):
        return None
    wanted_title = title_key(title)
    wanted_sources = frozenset(capture_ids)
    for row in connection.execute(
        "SELECT result_id, title, provenance_json FROM search_documents "
        "WHERE record_type = 'canonical' AND space_id = ? ORDER BY result_id",
        (space_id,),
    ):
        existing = cast(str, row["result_id"])
        if title_key(cast(str, row["title"])) == wanted_title:
            return existing
        if existing in sibling_page_ids:
            continue
        provenance = json.loads(cast(str, row["provenance_json"]))
        sources = frozenset(provenance.get("capture_ids") or (provenance["capture_id"],))
        if sources == wanted_sources:
            return existing
    return None
```

`provenance_json` for a canonical row is `{"capture_id": X}` or `{"capture_id": X, "capture_ids": [...]}` (`search_projection.project_search_document`).

- [ ] **Step 4: Refuse in `_decide` and thread the flag**

Add `duplicate_page_id` to the `from .review_bound import (...)` block in `review.py`.

`_decide` signature, add after `authority: EffectiveAuthority | None = None,`:

```python
        allow_duplicate: bool = False,
```

Inside the transaction's `else:` branch, directly after the existing check that raises `"page proposal requires routed capture"` and before `now = _timestamp(self._clock())`:

```python
                if (
                    outcome in (DecisionOutcome.APPROVED, DecisionOutcome.EDITED)
                    and not allow_duplicate
                    and cast(str, proposal["proposed_kind"]) == "page_update"
                ):
                    sibling_page_ids = frozenset(
                        cast(str, item["page_id"])
                        for item in connection.execute(
                            "SELECT page_id FROM proposals WHERE set_delivery_id = ? "
                            "AND proposal_id != ? AND page_id IS NOT NULL",
                            (proposal["set_delivery_id"], proposal_id),
                        )
                    )
                    if (
                        duplicate_page_id(
                            connection,
                            space_id=cast(str, proposal["space_id"]),
                            page_id=cast(str, proposal["page_id"]),
                            title=cast(str, proposal["title"]),
                            capture_ids=(
                                cast(Sequence[str], context[1]["provenance"])
                                if context is not None
                                else (cast(str, proposal["capture_id"]),)
                            ),
                            sibling_page_ids=sibling_page_ids,
                        )
                        is not None
                    ):
                        raise ValueError("duplicate canonical page")
```

The check runs inside the reservation transaction, so it sees every page published before this decision and leaves no reservation when it refuses.

`ReviewTasks.decide`: add `allow_duplicate: bool = False,` after `expected_review_digest` and pass `allow_duplicate=allow_duplicate,` to `self._engine._decide(...)`.

`contracts.ReviewTask.decide`: add `allow_duplicate: bool = False,` after `expected_review_digest: str | None = None,`.

- [ ] **Step 5: Service argument**

`review_publication.py`:

```python
_OPTIONAL = {
    "propose": {"target_page_id", "idempotency_key", "title", "markdown", "patch"},
    "list": {"capture_id", "space_id", "status", "limit", "offset", "facet_query_id"},
    "show": set(),
    "approve": {"idempotency_key", "allow_duplicate"},
    "reject": {"idempotency_key"},
    "edit_and_approve": {"idempotency_key", "markdown", "replacement_body", "allow_duplicate"},
}
```

In the validation loop, before the final `elif field == "idempotency_key" ...`:

```python
        elif field == "allow_duplicate":
            if type(value) is not bool:
                raise ReviewPublicationError("invalid_arguments")
```

In `_decide`'s `self._task.decide(...)` call add:

```python
                allow_duplicate=arguments.get("allow_duplicate") is True,
```

String map: add `"duplicate canonical page": "duplicate_page",`.

- [ ] **Step 6: MCP schema, CLI flag, messages, allowlists**

`local_mcp.py`, approve tool: pass `{**decision_properties, "allow_duplicate": {"type": "boolean"}}` instead of `decision_properties`, and change its description to:

```python
                    "Approve and publish the exact inspected review-token-bound draft. A new "
                    "note whose title or sources match a page in the same space is refused with "
                    "duplicate_page unless allow_duplicate is true.",
```

Edit tool: add `"allow_duplicate": {"type": "boolean"},` beside `"markdown"` and `"replacement_body"`. Reject keeps `decision_properties`.

`local_entrypoints.py` parser loop:

```python
    for action in ("approve", "reject", "edit-and-approve"):
        decision = children.add_parser(action)
        _add_local_options(decision)
        decision.add_argument("proposal_id")
        decision.add_argument("--review-token", required=True)
        if action == "edit-and-approve":
            edited_body = decision.add_mutually_exclusive_group(required=True)
            edited_body.add_argument("--markdown-file", help="UTF-8 full-page body path, or -.")
            edited_body.add_argument(
                "--replacement-body-file",
                help="Explicit complete replacement body for a patch proposal, or -.",
            )
        if action != "reject":
            decision.add_argument(
                "--allow-duplicate",
                action="store_true",
                help="Publish even if a page in the space has the same title or sources.",
            )
        decision.add_argument("--idempotency-key")
```

`_review_arguments`, in the final `else:` branch after the edit-and-approve body handling:

```python
        if getattr(parsed, "allow_duplicate", False):
            arguments["allow_duplicate"] = True
```

`_write_review_failure` messages:

```python
        "duplicate_page": (
            "A canonical page with the same title or the same sources already exists in this "
            "space. Update that page, reject this proposal, or repeat with --allow-duplicate."
        ),
```

`mcp_protocol.py`: add `"duplicate_page",` after `"replacement_contains_redaction",`.

`plugin_bridge.py`:

```python
_BRIDGE_REVIEW_CODES = {
    "review_target_stale": "review_conflict",
    "replacement_contains_redaction": "review_conflict",
    "duplicate_page": "review_conflict",
}
```

- [ ] **Step 7: Document it**

In `docs/review-publication.md`, after "Approval publishes one canonical page and makes it searchable." add:

```markdown
Approving or editing a new note is refused with `duplicate_page` when a canonical page in the same
space already has the same title (ignoring case and repeated whitespace) or exactly the same source
captures. The check runs at decision time, so a draft that found no match earlier is still checked.
Drafts proposed together from the same sources are compared by title only. To publish a deliberate
duplicate, add `--allow-duplicate` (CLI) or `allow_duplicate: true` (MCP approve and
edit-and-approve). The Obsidian plugin reports this refusal as `review_conflict` and has no override.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/services/test_review_publication.py packages/app/tests/integration/services/test_local_mcp.py packages/app/tests/integration/services/test_local_entrypoints.py -k "review or duplicate or sibling or safe_errors"`
Expected: all pass.

- [ ] **Step 9: Sweep existing suites for incidental duplicates**

Run: `uv run --frozen pytest -q -n auto packages/app/tests packages/engine/tests packages/collector/tests -k "review or publication or facet or slack or saved or patch or bridge or obsidian"`
Expected: pass. If a pre-existing test fails with `duplicate canonical page` or `duplicate_page`, read it. If it publishes two notes with the same normalized title or the same sources in one space as test scaffolding, give the second note a distinct title. If the duplicate is the behavior under test (for example a deliberate re-publication), pass `allow_duplicate=True` there. Do not weaken `duplicate_page_id`. List every changed test in the commit message.

- [ ] **Step 10: Commit**

```bash
git add packages/engine/src/open_brain_engine/engine/review_bound.py \
  packages/engine/src/open_brain_engine/engine/review.py \
  packages/engine/src/open_brain_engine/engine/contracts.py \
  packages/app/src/open_brain/services/review_publication.py \
  packages/app/src/open_brain/services/local_mcp.py \
  packages/app/src/open_brain/services/local_entrypoints.py \
  packages/app/src/open_brain/services/mcp_protocol.py \
  packages/app/src/open_brain/services/plugin_bridge.py \
  packages/app/tests docs/review-publication.md
printf '%s\n' 'feat(review): refuse duplicate pages when approving a new note' '' \
  'Approve and edit-and-approve return duplicate_page when a page in the same' \
  'space has the same normalized title or source set. allow_duplicate and' \
  '--allow-duplicate override it.' '' 'Refs #126' \
  > "$(git rev-parse --git-dir)/review-friction-msg.txt"
git commit -F "$(git rev-parse --git-dir)/review-friction-msg.txt"
```

<!-- model: sonnet:high -->

### Task 11: Phase B gates

**Files:** none modified unless a gate fails.

- [ ] **Step 1: Full verification**

Run: `make verify`
Expected: Ruff and MyPy clean, pytest with no failures, builds succeed, `make plugin-test` (inside verify) passes including `t03-wire.test.ts`.

- [ ] **Step 2: Native audit (budget check)**

Run: `make native-audit`
Expected: pass. `t03_schema.json` ships in the native base (`release/open-brain/open-brain.spec`), and the engine grew. On `artifact-invalid`, follow BUILD-021 and stop; do not raise `MAX_OBJECTS` in this PR.

- [ ] **Step 3: Whitespace, workflow lint, coordinates**

Run: `git diff --check fix/review-friction-a...HEAD && actionlint .github/workflows/ci.yml`
Expected: no output, exit 0.

Run: `git diff fix/review-friction-a...HEAD --stat -- packages/engine/src/open_brain_engine/engine/local_schema*.py packages/engine/src/open_brain_engine/engine/*migration*.py packages/engine/src/open_brain_engine/portable`
Expected: empty (schema 23, runtime 16 and Portable 18 untouched).

---

## Spec deviations

Places where the code forced a change from the owner decisions, with reasons:

1. **Reject skips the whole live-state check, not only the target.** `validate_current_binding` checks sources, routes and the target in one call, and rejection writes nothing, so Task 1 skips the call for `REJECTED`. The review-token (draft) check still runs.
2. **Facet (binding v5) proposals still validate on reject.** Their validator (`validate_facet_current_binding`) also enforces query and subject authority inside the reservation transaction. Skipping it for reject would drop an authority check, so a stale facet proposal still cannot be rejected. Follow-up candidate.
3. **"Which binding changed" reaches the CLI, not MCP.** The MCP error channel carries only an allowlisted code string (`mcp_protocol.py`), so MCP gets the distinct `review_target_stale` code but bare `review_conflict` for source, route and draft. The CLI prints a binding-specific message and `--json` adds `error.binding`.
4. **The plugin bridge keeps protocol-1 DTOs.** Facet `publication.show` and `publication.list` are parsed with exact key sets in the plugin, and `PluginBridgeFailure` raises on unknown codes. The bridge therefore strips `stale`, `replacement` and `page_diff`, and maps `review_target_stale`, `replacement_contains_redaction` and `duplicate_page` to `review_conflict`. Plugin users get no duplicate override. Recorded as OBSIDIAN-007.
5. **The redaction guard compares placeholder counts.** "Unless the same placeholder already exists" would let a merge add a second `[protected]` when the stored page has one. Counting refuses any added placeholder.
6. **The edit baseline for a new note is its stored draft.** No page exists yet for a create, so the guard compares against the raw draft the proposal stored.
7. **Sibling drafts are exempt from the same-sources rule.** One proposal set may hold up to 8 drafts from the same captures (an existing flow); the capture-set rule would block all but the first. The title rule still applies to siblings.
8. **Duplicate titles are compared against the indexed title.** `search_documents.title` is the projected title. A title containing a protected value may not match its raw form.
9. **`projection_applied` is optional in the T03 schema and also appears on `history.show`.** Optional keeps older fixtures and servers valid; `history.show` shares `paging.read_record`. A plugin copy installed before this change rejects the field until `open-brain obsidian-plugin install` refreshes it. This is the same trade-off the `selection` field took.
10. **The grant table offers search as a revision source.** `brain_search_page` results carry `revision_id`, so history-read is one of two ways to find a current revision, not a requirement.

## Self-review notes

- Spec coverage: item 1 (Tasks 1-3, 5), item 2 (Tasks 7-8), item 3 (Task 9), item 4 (Task 5), item 5 (Task 10), item 6 (Tasks 4-5), first-use coordinates (Task 5), gotchas (Tasks 1-2), gates (Tasks 6 and 11).
- Names used across tasks: `ReviewBindingConflict.binding`; `ProposalRecord.stale`; `ReviewProposal.stale/replacement/page_diff`; `target_head_advanced`; `projection_markers_added`; `PUBLIC_PROJECTION_MARKERS`; `ProjectedRecord.projection_applied`; `_bounded_page_diff`; `_edit_baseline_body`; `title_key`; `duplicate_page_id`; `_frozen_publication_result`; `_BRIDGE_HIDDEN_REVIEW_FIELDS`; `_BRIDGE_REVIEW_CODES`; public codes `review_target_stale`, `replacement_contains_redaction`, `duplicate_page`.
- Unverified until execution: whether other existing tests publish incidental duplicates (Task 10 Step 9 sweeps them), the native audit object budget after both phases (Tasks 6 and 11), and plugin vitest path filtering (`npm ... test -- tests/t03-wire.test.ts`); if the filter is not accepted, run `make plugin-test`.
