# OB1 standard-library content policy proposal

Date: 2026-09-09

Status: approved by the owner on 2026-09-09 and implemented in the bounded artifact auditor.
The owner replied "approve" to the explicit request for these two exact module/hash exceptions.

## Approved decision

The artifact-only exception covers `private-ip-address` findings in exactly these two expanded
PYZ module payloads. Module import identity and the complete expanded marshal SHA-256 must both
match. No module-name-only, package-wide, pattern-wide, or version-wide exception is proposed.

| Module | Expanded marshal bytes | SHA-256 |
|---|---:|---|
| `ipaddress` | 94,407 | `57a9a0e800670f6f7f44b51a5c1a3ccaa6e159d8d268c0db939ad096917d2f42` |
| `urllib.request` | 91,319 | `30e71da25ad6fa4f4eb5ceefff79e87c157105cfeed0527247cdee657c061188` |

The canonical private denylist, credential rules, home-path rules, forbidden paths/types, parser
validation, resource bounds, and all other findings remain enforced. Source and history policy are
unchanged. This is separate from the approved inherited-history exception.

Apply the disposition only after the bounded parser successfully validates and scans the complete
module. Remove only that module location's `private-ip-address` finding. Never suppress a finding
in the enclosing archive, PYZ table, another member, or another rule. A changed hash cannot use the
exception; new hashes require review and must never be added automatically.

## Review evidence

`ipaddress._IPv4Constants._private_networks` contains three RFC 1918 CIDR constants needed for runtime
address classification. They must retain their behavior. The earlier readiness follow-up described
all address findings as examples; these three are runtime data. `urllib.request` has one matching
docstring in `_proxy_bypass_winreg_override`.

The installed sources were fetched independently from CPython's `v3.14.4` tag and compared byte for
byte. The freshly bundled modules also compare equal to code compiled from those sources with
PyInstaller's filename normalization and optimization level zero. The fresh metadata-cleaned build
contains the exact two marshal payloads listed above.

| Official source | Source SHA-256 |
|---|---|
| [CPython ipaddress.py](https://raw.githubusercontent.com/python/cpython/v3.14.4/Lib/ipaddress.py) | `4cba28ab40dc34c685950535cf9adf6251bee5b63c82eed1f315d93853d4eaca` |
| [CPython urllib/request.py](https://raw.githubusercontent.com/python/cpython/v3.14.4/Lib/urllib/request.py) | `7465dbf037295f8f04774c453ab84c31879999c507fa16a82627ce9bd9615992` |

The three reviewed runtime-string digests are
`93997fe8a8121085052fd8c9a6515591714f1b745733ac379f691a9074d518a5`,
`59fb47b55edaafdb39c6a47128a8156ab33ea55dd29ebb53b4e3bd0c3900ba34`, and
`da3508145559327a834270888340bada8c5cb343aa2ed4c996276be352066030`.
The reviewed docstring digest is
`ae82384031228da83791438371aca5507170afe7cf6c36f64475932967b46a29`.
These string digests are supporting evidence, not independent exception keys.

Both old W7 CI archives contain a different `urllib.request` payload,
`567e733eef044092e919566a3afd9c9a14b7f1d80c8d07e232a5bacde8a994cc`. It is not covered by this
proposal. Rebuild both platforms and inspect their actual final payloads. The build currently
selects Python `3.14`, so patch updates may change hashes and require another review. Do not weaken
matching to accommodate that drift.

An independent read-only design review agreed with this scope and rejected removing runtime
constants or broad standard-library exemptions. This proposal does not attest arbitrary executable
behavior, authorize publication, or replace the owner audit of complete release archives.

## Options considered

1. Approve this exact provenance-backed disposition. This preserves upstream runtime semantics and
   confines the exception to reviewed public content. Recommended.
2. Keep the current zero-exception archive policy. The two findings remain release blockers.

Changing upstream constants, encoding them differently to evade the detector, or skipping complete
standard-library modules is not a sound alternative.

## Verification contract

Add contract tests proving that the approved hash/name/rule combination is the only suppressed
finding, modified bytes and wrong module identities retain findings, owner terms always fail,
other rules remain visible, malformed or over-limit artifacts still fail, and source/history scans
have no exception. Then run full verification and the owner audit against rebuilt platform archives.

The owner approval is recorded here and in the product plan's owner-only private-content checkpoint.
The implementation keeps upstream runtime code and constants intact. This decision does not approve
new hashes, pushing, merging, release creation, or tap publication.
