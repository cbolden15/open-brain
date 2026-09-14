# NW4 release acceptance checkpoint

Status: NEEDS WORK

Date: 2026-09-13

Candidate source commit: `896c208b5a8cceaf9501f37a1ec2d16418b4613e`

Version: `0.1.0`

## Result

NW4 has an implemented product boundary, an explicit contributor/native integration command, and a
reviewable macOS arm64 candidate resource pair. The local code, plugin, packaging, and Homebrew
journeys pass. Release acceptance remains open because the Linux candidate pair, exact-candidate CI,
real-provider timed desktop journeys, owner audits, and independent reviews are absent.

This checkpoint does not reopen the clean-Ubuntu investigation. The earlier boot blocker remains the
recorded desktop limitation. No disposable CI root staging, namespace, capability, or fixture-owner
topology entered the product.

## Implemented NW4 scope

| Requirement | Result | Evidence |
|---|---|---|
| Current product authority | PASS | `docs/product-family.md`, install/acceptance guidance, privacy model, threat model, artifact characterization, contributor guidance, README, and `CLAUDE.md` now describe the managed vault, desktop client, cloud authorization, Graphify boundary, and closed subscription path. Historical plans and audits remain unchanged. |
| Contributor composition | PASS | `make contributor-check` runs `make verify` and the explicit `make native-integration-smoke`; the latter delegates to the real paired-resource Homebrew journey. The release test asserts both plugin-test and native-smoke composition. |
| macOS candidate resources | PASS | Commit `896c208` produced the base and Graphify archives, two-row single-platform component manifest, provisional macOS formula, and machine-readable evidence record under `build/nw4-candidate-896c208/release/`. |
| Final two-platform assets | OPEN | No Linux resource pair is available for this commit, so a combined four-row manifest and two-platform formula cannot be prepared. |
| Timed desktop acceptance | OPEN | The earlier macOS GUI run was structural and untimed. No real OpenAI, Anthropic, or Gemini journey ran. No Linux desktop GUI journey passed. |
| Owner and independent review | OPEN | `PRIVATE_DENYLIST` is unset, so source, history, and exact-artifact owner audits were not run. Independent plan-conformance and safety reviews have not run. |

## Verification

| Check | Result |
|---|---|
| `uv run --frozen pytest -q tests/release/test_native_distribution.py` | PASS: 13 passed |
| `make verify` | PASS: Ruff; strict MyPy over 192 files; 1,011 pytest tests passed and 5 filesystem tests skipped; all wheels/source archives built; 18 plugin tests passed |
| `NATIVE_OUTPUT=build/nw4-native make native-integration-smoke` | PASS: capture, search, export, doctor, Markdown import, local MCP, workspace/Graphify projection, and plugin staging/removal; `existing_product: absent` |
| `NATIVE_OUTPUT=build/nw4-candidate-896c208 make native` | PASS: exact-commit macOS arm64 base/helper build, module/signature audits, internal native smoke, and component manifest |
| `actionlint .github/workflows/ci.yml` | PASS |
| `shellcheck tools/homebrew-smoke.sh` | PASS |
| `git diff --check` | PASS before the checkpoint commit |

The Homebrew run reported that the installed Xcode 26.6 and Command Line Tools are behind the newest
available release. Homebrew treated this as a support-tier warning; installation and every Open Brain
check still passed.

## Exact macOS candidate assets

Platform: macOS 26.3 build 25D125, arm64, normal user UID 501. Build runtime: CPython 3.14.7,
PyInstaller 6.22.2, PyInstaller hooks 2026.7, Homebrew 6.0.22-325-g06cc132. The installed Obsidian
bundle reports 1.12.7; the earlier GUI audit recorded the loaded Obsidian application as 1.13.7, so a
future timed run must record both the bundle and loaded application versions explicitly.

| Asset | SHA-256 |
|---|---|
| `open-brain-0.1.0-macos-arm64.tar.gz` | `e3b76fdf1b46a3da93ad2f97e98bdd75cd865cf4a5aa297ff5d8c0298c215a56` |
| `open-brain-graphify-0.1.0-macos-arm64.tar.gz` | `b66fc1b43067c52e36f6196d15a64b9d047ea9f8e8810a6170482dfcf206064d` |
| `open-brain-component-manifest-v1.txt` | `5052c9b22ec82cc0a0eee5b2425f237c66e11c12e020f5c18d97a2c141b4fc31` |
| provisional macOS-only `open-brain.rb` | `15d0c7ae9ad6abea158e2dbcaafa7e9f446a6f2840689df945ca3551e87c57c5` |
| `nw4-macos-candidate-evidence.json` | `7b0a2b202ec06cace3e1c10b9995b3030210a810162bbc12e72649dd847f6eaa` |
| plugin `main.js` | `4b17791ae64cb4dd865e6cd47e853428625bd17576a45daf153a4ef89e53da67` |

The formula is deliberately provisional and macOS-only. Publishing it would falsely imply a final
two-platform resource manifest.

## Rebuild observation

An earlier build from identical tracked content produced different executable, archive, and manifest
digests. The stable archive writer reproduces bytes for a fixed executable, while the complete macOS
PyInstaller/signing pipeline is not currently bit-for-bit reproducible across independent builds.
Only the `build/nw4-candidate-896c208/` bytes and digests above are selected for this checkpoint.
`INTEGRATION-032` records the operational rule.

## Remaining NW4 gates

1. Produce the Linux x86_64 base/helper pair for the same final source commit and combine all four
   archives into the canonical manifest and formula.
2. Push the branch and require the six protected exact-candidate CI checks to pass on its head.
3. Run the real-provider timed Obsidian matrix on macOS and the documented Linux desktop, recording
   exact candidate, versions, network conditions, elapsed time, export, and child cleanup.
4. Supply the uncommitted private denylist and run source, history, and exact four-archive owner
   audits without changing existing exceptions.
5. Complete independent plan-conformance and safety reviews against the exact candidate.

Publishing, tap changes, release creation, and merge remain separate actions.
