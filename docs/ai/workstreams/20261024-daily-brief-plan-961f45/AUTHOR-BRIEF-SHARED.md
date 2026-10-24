# Daily brief plan: shared author brief

You are one of three section authors writing the implementation plan for the daily work brief.
You write ONE package's tasks. An integrator merges the three sections into
`docs/superpowers/plans/2026-10-24-daily-brief.md` and runs the self-review.

## Source of truth

- Spec (authoritative, revision 2): `docs/superpowers/specs/2026-10-24-daily-brief-design.md`
- Worktree: `$WORKTREE`, branch
  `feat/daily-brief`, baseline commit `f6541c0`. It already contains the follow-ups work (#3,
  PR #119) you build on: `packages/engine/src/open_brain_engine/engine/followups.py`,
  `followup_contracts.py`, `followup_visibility.py`, `packages/app/src/open_brain/services/followups.py`,
  and the follow-up grants in `local_mcp.py`, `local_entrypoints.py`, `agent_setup.py`,
  `plugin_bridge.py`.
- Format exemplar: `docs/superpowers/plans/2026-10-23-followups.md`. Copy its task shape (Files,
  Interfaces, numbered TDD steps with real code, exact run commands with expected output, commit
  step), its "Verified baseline facts" habit, and its level of completeness. Read its header and
  one full task before you write.
- Project rules: `CLAUDE.md` in the worktree. The engine cannot import the app. Coordinates stay
  23/16/18: no schema, Portable or runtime-session change. No model call, network, subprocess or
  background work in the brief path.

## Hard rules

1. Read-only on the repository. Write ONLY to your output file. Do not edit, create, commit or
   stash anything in the worktree. Do not run `make verify` or the test suite; `rg`, `sed -n`,
   `git log`, `git show` and `uv run python -c` on existing code are fine.
2. Verify every path, symbol and line range you cite with `rg` against the worktree, and record
   the facts you relied on under a `### Verified baseline facts` heading at the top of your section
   (as the exemplar does). Do not cite a function you did not find.
3. No placeholders. Every code step shows the code. Every test step shows the test. No "similar to
   Task N", no "add validation", no TBD.
4. Synthetic fixtures only. No real Brain, Slack, credentials, `~/.dev-secrets.env`, `~/.claude` or
   `~/.codex` content. Never put hostnames or private paths in plan text.
5. Search with `rg`, never `grep -E`. Never call WebFetch.
6. Use exactly the shared interface names below. If you must deviate, put the deviation under a
   `### Interface deviations` heading at the very top of your output so the integrator reconciles
   it, and still use the shared name everywhere else.
7. Use your assigned task number range. Fewer tasks is fine; say which numbers are unused.
8. Each task is one implementer's unit: its own failing test, implementation, passing run and
   commit. Fold docs and wiring into the task that needs them. A task should be reviewable on its
   own by someone who reads only that task plus this section's Interfaces blocks.
9. Commit steps list exact `git add` paths and a conventional-commit message. The implementer adds
   the project's trailer; do not write trailers into the plan.
10. Finish by writing the file, then reply with ONLY: the output path, the task numbers you used,
    the line count, and any interface deviations (or "none").

## Shared interface contract (fixed names)

### Package 1 produces (Tasks 1–5)

`packages/app/src/open_brain/services/brief_contracts.py` (pure: enums, frozen dataclasses,
limits, wire shapes; no engine import beyond `t03_contracts` types):

```python
class BriefItemKind(StrEnum):
    FOLLOWUP_DUE = "followup_due"
    FOLLOWUP_NEW_EVIDENCE = "followup_new_evidence"
    FOLLOWUP_OPEN = "followup_open"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_STALE = "source_stale"

class BriefChange(StrEnum):   # the `changed` field
    NEW = "new"; STILL_OPEN = "still_open"; CHANGED = "changed"

class BriefReasonCode(StrEnum):
    OVERDUE = "overdue"; DUE_TODAY = "due_today"; NEW_EVIDENCE = "new_evidence"
    OPEN_OWNED = "open_owned"; CAPTURES_UNAVAILABLE = "captures_unavailable"
    NO_CAPTURE_SINCE = "no_capture_since"

@dataclass(frozen=True)
class BriefItem:
    kind: BriefItemKind; ref: str; reasons: tuple[BriefReasonCode, ...]; reason_text: str
    evidence: tuple[str, ...]; changed: BriefChange; rank: int
    def to_wire(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class BriefLogCoverage:  entries: int; bytes: int; unavailable: bool
@dataclass(frozen=True)
class BriefCoverage:
    families_checked: int; families_stale: int; captures_unavailable: int
    followups_scanned: int; followups_truncated: bool
    followups: Literal["read", "not_granted", "failed"]
    freshness: Literal["read", "failed"]
    collector: Literal["not_read"]
    hidden_dropped: int
    log: BriefLogCoverage
    def to_wire(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class BriefScope:  space_ids: tuple[str, ...] | None; tiers: tuple[str, ...]   # None == all

@dataclass(frozen=True)
class Brief:
    generated_at: str; scope: BriefScope; items: tuple[BriefItem, ...]
    nothing_needs_attention: bool; since: str | None; coverage: BriefCoverage
    model: Literal["none"]; saved: bool
    def to_wire(self) -> dict[str, object]: ...

class BriefError(Exception):
    code: Literal["invalid_request", "unsupported_capability", "operation_pending",
                  "version_conflict", "not_found"]

BRIEF_MAX_ITEMS = 5
BRIEF_MAX_FAMILY_ITEMS = 2
BRIEF_FOLLOWUP_SCAN_LIMIT = 500
BRIEF_RESPONSE_MAX_BYTES = 32 * 1024
BRIEF_LOG_MAX_BRIEFS = 30
BRIEF_LOG_MAX_FILE_BYTES = 64 * 1024
BRIEF_LOG_MAX_KEYS = 16
BRIEF_STALE_DAYS_DEFAULT = 7
BRIEF_STALE_DAYS_MIN = 1
BRIEF_STALE_DAYS_MAX = 365
BRIEF_GRANT_READ = "brief-read"
BRIEF_GRANT_ACK = "brief-ack"

def parse_family_ref(ref: str) -> str          # "family:<id>" -> id, else BriefError("invalid_request")
def parse_followup_ref(ref: str) -> str        # "followup:<id>" -> id
```

`packages/app/src/open_brain/services/brief_authority.py`:

```python
@dataclass(frozen=True)
class BriefSessionAuthority:
    authority: EffectiveAuthority          # the clamped authority every provider read uses
    brief_grants: frozenset[str]           # subset of {"brief-read", "brief-ack"} held
    followup_grants: frozenset[str]        # subset of the three followup-* grants held

def brief_session_authority(...) -> BriefSessionAuthority
    # same inputs as services.followups.followup_session_authority plus the brief grant flags;
    # applies followup_session_authority first, then records grants. Owner CLI path: owner
    # authority with every grant recorded as held.

def visibility_key(session: BriefSessionAuthority, *, selected_space_ids: tuple[str, ...] | None) -> str
    # sha256 hex over canonical JSON (sort_keys, separators (",", ":")) of:
    # {"owner", "allowed_read_tiers" sorted, "space_ids" sorted or "all", "egress_mode",
    #  "provider_id", "brief_grants" sorted, "followup_grants" sorted, "selected" sorted or "all"}
```

`packages/engine/src/open_brain_engine/engine/source_freshness.py` (engine, read-only task):

```python
@dataclass(frozen=True)
class FamilyFreshness:
    family_id: str                 # "consumer:<namespace>:<space_id>" or "markdown:<space_id>"
    kind: Literal["consumer_namespace", "markdown_import"]
    space_id: str
    newest_recorded_at: str        # ISO-8601 UTC from source_revisions.recorded_at
    capture_count: int
    unavailable_count: int         # availability in {missing, inaccessible}
    stale: bool                    # newest_recorded_at older than stale_days under the engine clock

@dataclass(frozen=True)
class FreshnessReport:
    families: tuple[FamilyFreshness, ...]
    stale_days: int
    generated_at: str

class SourceFreshnessTasks:
    def __init__(self, engine) -> None: ...
    def freshness(self, authority: EffectiveAuthority, *, space_ids: frozenset[str] | None,
                  stale_days: int) -> FreshnessReport
    # requires owner or capability "brief-read" via authority.require(...); honours tier, space,
    # lifecycle (retired excluded) exactly as RecordProjector reads do; manual captures excluded;
    # reads source_namespaces, logical_sources, source_revisions only; raises T03Error on
    # unsupported_capability / invalid_request (stale_days out of range).
```

Wired as `engine.source_freshness` beside `engine.followups`. The `brief-read` capability string is
registered wherever `followup-read` is, so `authority.require("brief-read")` works for a granted
agent authority.

### Package 2 produces (Tasks 6–14)

`packages/app/src/open_brain/services/brief_log.py`:

```python
@dataclass(frozen=True)
class FamilyFingerprint:  newest_recorded_at: str; unavailable_count: int
@dataclass(frozen=True)
class BriefLogEntry:      generated_at: str; items: tuple[dict[str, object], ...]; coverage: dict[str, object]
@dataclass(frozen=True)
class BriefLogFile:       key: str; briefs: tuple[BriefLogEntry, ...]; acks: dict[str, FamilyFingerprint]; sha256: str | None
@dataclass(frozen=True)
class BriefLogRead:       file: BriefLogFile | None; unavailable: bool; entries: int; bytes: int

class BriefLogStore:
    def __init__(self, state_dir: Path, *, clock) -> None   # files at state_dir / "briefs" / f"{key}.json"
    def read(self, key: str) -> BriefLogRead                 # corrupt -> rename to <key>.json.aside, unavailable=True
    def append(self, key: str, entry: BriefLogEntry, *, expected_sha256: str | None) -> bool   # CAS; False on conflict
    def ack(self, key: str, family_ref: str, fingerprint: FamilyFingerprint, *, expected_sha256: str | None) -> bool
    def last(self, key: str) -> BriefLogEntry | None
```

`packages/app/src/open_brain/services/brief.py`: `compose_brief`, `brief_generate`, `brief_ack`,
`brief_last` (owner CLI only), `bind_brief_operations` (mirrors `bind_followup_operations`), wire
shapes via `Brief.to_wire()`. MCP tools `brain_brief` and `brain_brief_ack`; flags
`--allow-brief-read` / `--allow-brief-ack` on `open-brain mcp` and `agent setup`; plugin bridge
forwarding; owner CLI `open-brain brief [--space NAME ...] [--stale-days N] [--json]`,
`open-brain brief ack FAMILY_REF [--json]`, `open-brain brief last [--space NAME ...] [--json]`.

### Package 3 produces (Tasks 15–18)

Acceptance module `packages/app/tests/acceptance/test_daily_brief_acceptance.py` (confirm the
acceptance directory name with `rg`), `docs/daily-brief.md`, reference-doc and both `CLAUDE.md`
grant lists, documentation gate entries, architecture-boundary allow-list for `services/brief.py`
and `brief_log.py`, and the final frozen-tree gate task (`make verify`, `make native-audit`,
`make homebrew-smoke`, `git diff --check`, `actionlint .github/workflows/ci.yml`).
