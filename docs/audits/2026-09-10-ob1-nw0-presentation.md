# NW0 presentation decision: Canvas overview and checked review

Status: the presentation decision passed scoped independent review. The integrated desktop proof
is still open. This is an NW0 contract, not a shipping plugin or a completed NW0-D result.

## Selected presentation

Use a rebuildable Canvas overview alongside a plugin-owned command or suggestion list that opens
the checked review modal. The user selects a current opaque suggestion identity. The modal resolves
it against the current immutable publication, displays checked evidence and revision/staleness,
offers validated local source navigation, and requires preview plus explicit owner acceptance before
recording a permanent link. Inferred edges remain visually distinct from accepted links.

There is no promise that clicking a built-in Canvas edge opens the modal. The pinned official
Canvas type snapshot describes serialized nodes and edges, not a public edge-selection callback or
Canvas view API. D13's existing entry point uses `Plugin.addCommand`. A supported command/list route
must therefore be proved as its own integration, rather than inferred from static Canvas rendering.
See the [official Canvas types](https://github.com/obsidianmd/obsidian-api/blob/master/canvas.d.ts)
and [plugin event documentation](https://docs.obsidian.md/Plugins/Events).

The proper unified-interaction alternative is a plugin-owned graph `ItemView`. It owns graph clicks
and review controls without relying on built-in Canvas internals, but replaces Canvas rendering.
Its feasibility, accessibility, keyboard behavior, rendering performance, lifecycle and packaged
behavior remain unproved. It is not selected or implemented by this decision.

The locally bundled HTML comparator remains private reference evidence. A future HTML export or
accessibility fallback is possible but is not a first-release commitment. That optional format does
not qualify or replace mandatory Portable Brain export, validation, import, reconciliation and
retrieval.

## Options and evidence

| Option | Disposition | Evidence or missing proof |
|---|---|---|
| Canvas overview plus owned command/list and checked modal | Selected candidate | D12 observed static Linux Canvas/navigation; D17 observed the isolated Mac modal journey. The combined current-suggestion route still needs proof. |
| Plugin-owned graph `ItemView` | Proper unified alternative, pending feasibility | Owns its interaction contract; requires rendering, accessibility, lifecycle and desktop checks. |
| Modal alone or static Canvas alone | Partial proofs only | Neither alone covers both an immediately visible overview and checked acceptance. |
| Built-in Canvas hooks through private DOM/internal APIs | Not selected | No supported API basis is established; an exact-version private hook would require a separate decision and compatibility proof. |
| Bundled offline HTML | Comparator only | Source inspection and bounded fixtures are not desktop activation or action-authority evidence. |

D17's retained deterministic journey includes evidence, both endpoint navigations, revocation,
one explicit acceptance, export/restore, inactive consent on reopen and unchanged Markdown. Its
accepted-link fixture must not be reset. D12 covers the static Canvas and navigation only. Neither
receipt proves that the selected combined presentation already works. The corrected D21 decision
and its independent review preserve this distinction and keep all earlier failed drafts.

## Authority and later owners

| Owner | Responsibility after NW0 freezes the contracts |
|---|---|
| NW1 | Durable revisions, consent/revocation, exclusions, policy generation, shared budgets, provenance, idempotent acceptance, journal recovery, Portable operations and inference authorization. |
| NW2 | Immutable graph publications, Canvas generation, inferred/accepted styling, source mapping, staleness and generated-output exclusion. |
| NW3 | Onboarding, supported events/command/list/modal, checked text and local navigation, preview/accept controls, refresh pause/manual controls and unload. Consent controls invoke NW1; the pause preference never grants authority. |
| NW4 | Final exact-release-candidate journey on both desktops and all required provider paths within the unchanged time limit. |

The UI supplies an opaque suggestion identity, not authoritative bodies, quotations, paths,
revisions or owner labels. The service resolves that identity against the immutable publication and
rechecks current authorization and accepted state at acceptance. Canvas and HTML are projections,
not authority. Inference does not modify Markdown; the separate
[representation decision](2026-09-10-ob1-nw0-representation.md) governs materialization.

## Remaining NW0 proof

The exact packaged disposable candidate must run on both target desktops. Cover generation/opening
of Canvas, inferred/accepted distinction, current suggestion selection through the supported
command/list, checked evidence and both navigations, stale/revision rejection, one explicit
acceptance, retained state and clean unload. Inference, refresh, display, preview, revocation and
stale rejection must leave Markdown unchanged. Any explicit acceptance materialization must add
only the previewed permanent link, preserve concurrent edits and report conflicts or unconfirmed
outcomes. D17's unchanged-Markdown receipt covers its record-only acceptance, not a write-back
proof. Record activation/trust prompts, local asset closure, runtime downloads, cold/reused timing
and before/after fingerprints.

The full thin slice still requires capture, inference, evidence, preview, acceptance, verified
Portable export and validation, import, authoritative reconciliation and retrieval. Automatic
batched refresh, durable pause/manual behavior and visible publication require integrated desktop
evidence. Headless event/DOM doubles cannot substitute for that journey. Required real-adapter
samples and independent final coverage remain open; see the
[continuation record](2026-09-10-ob1-nw0-continuation.md).

NW1/NW3 productization and NW4 final release acceptance remain later obligations, not additional
NW0 implementation. This decision neither closes NW0 nor authorizes early shipping work.
