# Five-minute Open Brain acceptance test

Status: current target contract; command-line integration and macOS plugin structure are proven;
exact-candidate real-provider timing on macOS and Linux desktop timing remain open

Date: 2026-09-13

## Supported starting point

Run each journey from a regular user account on one of these hosts:

- macOS on Apple Silicon (`arm64`)
- Linux on `x86_64`; the project currently uses an identified Ubuntu UTM guest, so its timing is
  emulated evidence rather than native-hardware evidence

Homebrew must already be installed and available as `brew`. Installing Homebrew is outside the
product clock. The selected provider account and API key must be usable. Network access must reach
Homebrew, GitHub Releases, the Obsidian download host when Obsidian is missing, and only the selected
provider endpoint during semantic refresh.

The account starts without Open Brain data or an installed Open Brain formula. Record whether
Obsidian was absent, newly installed, or reused. A reused Obsidian installation must not already have
the Open Brain plugin enabled in the acceptance vault.

## Candidate identity

Before starting, record all of these values:

- Git commit and Open Brain version;
- base archive filename and SHA-256;
- Graphify archive filename and SHA-256;
- component manifest SHA-256;
- plugin ID, version, minimum Obsidian version, and `main.js` SHA-256;
- Open Brain, Graphify, Obsidian, operating-system, architecture, and Homebrew versions;
- selected provider, access mode, account prerequisite, network conditions, and whether credentials
  use the session or operating-system store.

The base archive, Graphify archive, component manifest, and Homebrew formula must refer to one
version. Use the exact files under review. A local formula may substitute file URLs before the public
release exists, but it must retain the same resource layout and digests.

## Timed desktop journey

Run the journey once for each supported direct access path on each platform: OpenAI API key,
Anthropic API key, and Google Gemini API key. Claude subscription is outside this candidate because
the product fails it closed with `subscription_isolation_unproven`. Do not add privileges, root
staging, namespaces, capabilities, alternate fixture ownership, or wider host access to make that
transport run.

Use a bounded synthetic corpus of two related, previously unlinked notes and one unrelated
distractor. Start an ordinary monotonic wall-clock timer immediately before installing the candidate.
Do not pause it for downloads, Obsidian activation, API-key entry, or provider latency.

1. Install the paired candidate resources through Homebrew. Install and launch Obsidian if needed.
2. Run `open-brain workspace setup` and `open-brain obsidian-plugin install`. Open `Open Brain Vault`,
   enable the Open Brain community plugin, and run **Open Brain: Initialize or locate managed vault**.
3. Capture the three synthetic notes. Configure the selected provider, review the eligible-note
   scope, choose session or OS credential storage, and run **Open Brain: Refresh graph now**.
4. Open `Open Brain Graph.canvas`. Verify an inferred edge joins the related notes, its source
   evidence is shown, and source navigation opens the original Markdown note. Verify no permanent
   wiki link was written by inference.
5. Edit and save a source note. Refresh, review the current suggestion, preview the proposed link,
   accept it, and retrieve the edited text with Open Brain search. Export with
   `open-brain export <new-directory> --verify` and stop the timer.

## Pass conditions

The journey passes only when the recorded elapsed time is at most 300 seconds and all conditions
below hold:

1. Homebrew verifies and installs the base and Graphify archive digests, the plugin loads only after
   explicit activation, and all processes run as the regular user.
2. The first operation creates the private Brain and managed sibling vault without a storage prompt,
   configuration file, database command, listener, daemon, service, container, or elevated setup.
3. Only eligible accepted notes reach the selected provider after explicit consent. The distractor
   behaves according to the declared fixture policy, no second provider is contacted, and no raw key
   appears in arguments, settings, vault files, logs, output, or export.
4. Structural and inferred edges remain distinct. Inference alone does not edit Markdown. Accepting
   the reviewed current suggestion writes one permanent link through the revision flow; stale
   acceptance is rejected.
5. Search returns the saved edit. The verified Portable Brain export contains the accepted revision
   and suggestion provenance, but contains no credential, local path, plugin setting, generated
   Canvas, or graph cache.

Closing or disabling the plugin must stop its `open-brain plugin` child. The Obsidian desktop process
may remain open and must be reported separately from Open Brain's no-background-service guarantee.

## Command-line integration coverage

`make contributor-check` is the contributor and hosted-CI gate. It runs `make verify`, which includes
the real plugin typecheck, production build, and tests, followed by `make native-integration-smoke`.
The native smoke builds and audits both executables, renders a temporary keg-only formula, installs
the paired resources and plugin assets, and exercises CLI capture/search/export, Markdown import,
MCP, managed workspace, structural Graphify projection, and plugin staging/removal.

CI runs that command on `macos-latest` after asserting `arm64` and on `ubuntu-latest` after asserting
`x86_64`. It records ordinary job duration and uploads the platform build output. This proves native
command-line behavior on both hosted runners. It does not prove that Obsidian opened, the plugin
activated, a real provider completed inference, source navigation worked in the GUI, or the
five-minute desktop clock passed.

## Current evidence and open gate

The merged NW3 candidate at `b5a0e6d` passed the full local contributor check on macOS arm64. Obsidian
1.13.7 also demonstrated plugin activation, capture/search, source navigation, one reused Canvas tab,
and plugin-child cleanup on macOS using synthetic data. Those runs were structural and were not
timed exact-candidate journeys with a real provider.

No Linux desktop GUI/provider journey is accepted. The earlier clean-Ubuntu attempt ended at a
documented boot blocker, and its disposable CI root staging or ownership topology is not product
architecture. Ubuntu installation by itself is not acceptance evidence. The release acceptance gate
therefore remains open until exact-candidate macOS and Linux desktop records meet the pass conditions
or the product scope is explicitly changed.

## Evidence record

For each run, record:

```text
candidate_commit:
product_version:
base_archive: <filename> <sha256>
graphify_archive: <filename> <sha256>
component_manifest_sha256:
plugin: open-brain <version> minAppVersion=<version> main_js_sha256=<sha256>
platform: <os/version/architecture; identify emulation>
runtime: <open-brain/graphify/obsidian/homebrew versions>
starting_state: <Open Brain absent; Obsidian absent/new/reused>
provider: <name> access=api_key custody=<session|os>
network: <declared reachable endpoints/conditions>
timer_started_utc:
timer_stopped_utc:
elapsed_seconds:
result: <pass|fail|blocked>
failed_step_or_blocker:
isolation: <uid; no root/capabilities/namespaces/listener/service/container/host sharing>
export_verification:
plugin_child_after_unload:
```

Retain command output and GUI captures beside the record. Redact API keys, account identifiers, note
content beyond the public fixture, local usernames, and private host paths.

## Deliberate exclusions

This acceptance test does not cover curl installation, Docker, certificates, multi-user grants,
arbitrary existing-vault synchronization, local Ollama models, Claude or Codex subscription access,
notarization, browser-downloaded Gatekeeper behavior, full-vault indexing, or a factory-clean
operating system.

`brew uninstall open-brain` removes Homebrew-managed product files. It does not remove the Brain or
managed vault. Data removal is a separate explicit action.

Open Brain relies on the operating-system account and disk protections. It does not claim
application-level encryption, hostile same-user isolation, custody, compartments, certified purge,
fencing, or recovery guarantees.
