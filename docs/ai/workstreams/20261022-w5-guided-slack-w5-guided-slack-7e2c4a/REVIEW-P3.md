# P3 review (independent, security-weighted)

Verdict: PASS_WITH_FIXES, no Critical. No path found where the generated policy or setup is broader than confirmed; policy is exact launcher-policy.v1; setup grants only search/content-read with no launch; requests persisted before authority calls; policy/setup apply halt-and-repreview; external reads see only approved copies (`paging.py:124`).

Sent back to the P3 implementer:
1. Important: destination dedication checked only at choice; recheck at policy-preview, policy-apply, setup-apply.
2. Important: dedication check is direct SQL; replace with an engine/owner read or a narrow owner-only engine read recorded in REANCHOR-P3 (or refuse existing spaces in v1).
3. Minor: existing-policy reuse must match guide principal/session IDs and 0600/0700.
4. Minor: post-write consent `unsafe_consent_state` must be `operation_unknown`, not refused.
5. Minor: refuse duplicate release-preview for an already-approved source at current head.
6. Minor: short-write loop in policy create.

Accepted: private `_read_policy` import (P4: public name); halted-journal cancel; deviations 2, 3, 5, 6, 7; deviation 4 after fix 4.

Assigned to P4 (in addition to REVIEW-P2 items):
- Public name for the launcher policy reader used by the helper.
- Define when a journal becomes `completed` (today stays `active` after setup-apply).
- Cross-journal race test (two journals, same Brain/source).
- Status: optionally show recorded release progress, labelled as recorded journal progress, per HEALTH.md.
