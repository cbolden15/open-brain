# Portable Brain v12

Slack owner-approved Work evidence uses local state schema 17, runtime session 12
and Portable metadata 12. W1a and W1b form one activation boundary: a schema-17
runtime must include the Portable-12 validator and restore path. This is an
unreleased local candidate. It does not authorize a real-source migration or
provider-backed client setup.

## Retained evidence

Portable 12 retains the inherited Portable-11 source, privacy, issuer, lifecycle,
admission, historical, facet and development evidence. It preserves saved-Markdown
v1 sharing bytes and public-copy semantics. The sharing authority sidecar also
admits the explicitly versioned Slack Work variant, whose copies keep Work privacy
and bind approved providers, exact projected message text and citation metadata.
Original Slack records retain their own local-only privacy decisions.

The required `history/sharing/slack-recall-selections-v1.json` sidecar retains
immutable ordered logical-source membership and each creation operation's exact
request, canonical selection bytes and digests. A selection contains at most 24
distinct logical sources. Member revisions are approved separately; reapproving
one edited member does not change the selection or the other approvals.

Slack sharing validation binds canonical source-intake and revision receipts,
original capture bytes, preview and inspection commitments, owner decisions,
Work-copy privacy, canonical envelope projection, replay/linkage evidence and
revocations. A permalink is approved citation metadata, with a narrow field-local
exception to ordinary source-reference redaction. Message text remains untrusted
and passes through ordinary protected-value projection.

## Validation and restore

Portable-12 dispatch explicitly enables the Slack sharing variant. Earlier
validators retain their v1 sharing behavior and refuse the new evidence marker;
relabeling a new archive as an older version does not establish compatibility.
Required evidence is checked after manifest inventory and checksum validation.
Removing or substituting evidence and rehashing the manifest must still fail
before destination materialization.

Restore installs validated inherited evidence and the selection ledger into a
current disposable store. Reopen and re-export must preserve the retained bytes
and authority relationships. Search and complete reads still check current
source, route, lifecycle, approval, provider and copy state; restoring a historical
approval does not make stale or revoked evidence externally readable.

Supported older Portable imports remain supported. Standalone Portable 4 remains
refused. Frozen historical resource bytes are not rewritten to current coordinates.

## Migration and owner operations

The schema-16-to-17 migration requires exclusive runtime admission and settled
historical state. It checks settlement inside the migration transaction and before
commit, appends the selection ledger, and advances the runtime compatibility floor
to 12/17. Runtimes with incompatible coordinates must refuse schema 17. Current
clients require the exact state/runtime pair 17/12 and refuse the previous 16/11
pair.

Use synthetic disposable Brains for implementation checks. Before any separately
authorized private rollout, export with the currently installed matching binary,
retain collector state separately and prove that backup restores with that same
binary. Portable Brain does not include collector credentials, schedules, queues
or source-selection state. An older executable is not a rollback mechanism for a
migrated Brain.

W2 scoped setup and W3 cited-recall plumbing are accepted synthetic repository work.
W4 preserves 17/12/12 without a migration. The explicit-policy Slack Work gate is
new-runtime behavior: an older binary already using 17/12/12 is not fenced by
these coordinates and does not acquire that gate. Stop older sessions and use
the exact candidate consistently. This does not confine a hostile same-user process.

Use the [owner-run pilot checklist](integrations/slack-decision-recall.md#owner-run-private-pilot-checklist)
before any candidate opens the real Brain. Actual-client checks remain `not_run`;
private rollout requires separate approval and recorded restricted-client
injection-resistance evidence. No real Slack/provider acceptance is claimed.
