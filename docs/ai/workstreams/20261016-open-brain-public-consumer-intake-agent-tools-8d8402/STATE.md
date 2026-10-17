# Workstream State

- ID: `20261016-open-brain-public-consumer-intake-agent-tools-8d8402`
- Repo root: /Users/calebbolden/Projects/oss/open-brain-public
- Remote identity SHA-256 fingerprint: `51c3141e8dd7ccbb712aa075e0b4c373f3894d9e9b386c6db8f4301edc6bdfab`
- Worktree: /Users/calebbolden/Projects/oss/open-brain-public/.worktrees/consumer-intake-agent-tools
- Branch: feat/consumer-intake-agent-tools
- Objective: Expose owner-authorized consumer intake to Codex and Claude Code through a strict foreground CLI
- Created date: 2026-10-16

Milestone: supported owner CLI for agent invocation. Existing MCP adapters have no consumer tools; engine intake requires owner authority. Chosen architecture: strict normalized bundle over bounded stdin to a new consumer-import owner CLI with preview/body/commit/status/recover/halt. Mutations require --confirm; plan supplies no trusted authority. Existing-only bootstrap; no automatic recovery. Both provider profiles stay inactive, no private exports/live clients/config edits/network/deploy/push. Alternative proper delegated MCP design requires a separately scoped grant/owner approval design and is deferred rather than reusing capture/search grants.
Verification: real both-provider CLI roundtrip with test-only profiles, malformed/extra/duplicate/oversize input refusal before bootstrap, read-only status/body/no recovery, inactive-profile refusal, durable replay/recover/halt, MCP not advertised; make verify/tier audit/diff/actionlint.
Worker ledger: max active3,total3; reserved active1,total1 for read-only design/authority review. Coordinator owns all edits/commands/git. Stop: integration verified, locally committed, no push absent authorization.

Implementation checkpoint: six owner CLI leaves,1MiB strict stdin envelope, canonical object hashes/exact closure, explicit mutation confirmation, existing-only current-schema admission, escaped body-page output and unmodified engine DTOs. No MCP grant/handler or schema/runtime/Portable changes. Both production profiles remain inactive; no installed-runtime/config changes.
Independent read-only design and final diff review: no material findings; follow-up mixed-exit delta independently checked. Worker ledger active0/3,total1/3.
Focused verification:64 passed in6.88s across consumer CLI/retraction CLI/catalog; mypy742 passes; full Ruff,diff/actionlint pass. Test-only initial assumptions corrected: accepted pending phase is materializing and parser catalog adds6 leaves (104total). Test-tier audit8216 complete/disjoint before final mixed-result case; rerun final audit required.
Decision: owner shell bridge preserves MCP authority; delegated MCP permissions deferred. App1MiB limit explicit and smaller than engine64MiB; no archive parser claimed. Commit/recover/halt --confirm is invocation choice, not human-presence proof.
Next: freeze inputs,run make verify and final tier audit; record exact results; local commit only,no push. Native release files/specs/metadata are unchanged; native audit is not a substituted claim for source CLI tests.

First full gate: make verify exited2 after35:01 Python:8211passed,5filesystemskips,1existing fork warning;one exact architecture inventory failure for newly added consumer_import.py. This was a branch inventory omission, not an environmental/flaky failure. Added precisely that intended foreground module;5/5 architecture checks pass without weakening set equality. BUILD-014 prevention record updated.
Build/client gate: all4 Python wheel/sdist builds pass. Initial plugin gate failed8/700 because this fresh checkout lacked .venv/bin/open-brain (documented source-child prerequisite); created ignored actual .venv directory with bin alias to /tmp/open-brain-agent-intake-venv. Full unchanged make plugin-test desktop-test then passed700 plugin,286 UI,30 Python and26 Rust plus typecheck/build/fmt. Original failed attempt preserved.
Actual source entrypoint smoke:6 CLI leaves discoverable;both production providers refuse unsupported_profile;unknown operation body-free status refuses;no live agent configuration or private Brain accessed. Initial /var and /tmp aliases refused existing path confinement;canonical /private temporary fixture passed. Installed /opt/homebrew/bin/open-brain does not yet recognize the new command;no installed upgrade attempted.
Candidate checkpoint before final full rerun: all authored source and prior8211 passing tests unchanged since first full run;only exact inventory fix and prevention/receipt notes added. Coordinator runs final full make verify with bounded3workers and failed-only temporary retention. Worker ledger reserved active1/3,total1/3 for independent inventory-delta review. No native spec,module policy,metadata,dependencies or compatibility coordinates changed.
Exact final-head result/handoff will be recorded under ignored build/workstreams/20261016-open-brain-public-consumer-intake-agent-tools-8d8402/ after the full gate, to preserve frozen acceptance inputs. No push or installed-client cutover authorized by this milestone.

Independent inventory-delta review complete: no material findings;exact equality/prohibited checks intact. Worker ledger active0/3,total1/3. Final full rerun follows this candidate checkpoint.
