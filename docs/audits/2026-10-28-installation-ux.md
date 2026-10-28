# Installation and first-use audit

Date: 2026-10-28. Source baseline: `8c74298`.
Audience: personal users and developers running from a checkout.
Scope: reach a successful local capture and retrieval, then offer AI-client and
Obsidian setup. Connected sources and provider onboarding are separate journeys.

## Assessment

The local capture/search path is short: install, capture, search. It needs no
account, API key, configuration file, database command or separate initialization.
The documentation makes the wider setup harder to understand than those three
commands suggest. It mixes the published release, current source and historical
acceptance fixtures, then sends users between several optional setup guides.

That short path assumes Homebrew already works. A first-time machine still needs
that prerequisite, and the supported native platforms are macOS Apple Silicon
and Linux x86_64. This audit does not establish the time to install Homebrew or
measure a factory-clean computer. End users do not need the contributor toolchain.

There is no general first-run questionnaire in the baseline. The core chooses its
storage defaults automatically. Agent permissions are command-line flags, all off
by default; configuration writes require a preview followed by an explicit apply.
Import has a confirmation prompt. The separate source guide has an interactive
workflow, but is not a general installation wizard. Obsidian provider selection
and credential-storage prompts belong to optional graph inference.

## What a user does today

| Journey | Steps and prerequisites | What establishes success |
|---|---|---|
| Personal local memory | Homebrew already available; `brew install cbolden15/tap/open-brain`; `open-brain capture 'My project is Copper Orchard.'`; `open-brain search 'Copper Orchard'` | Search returns the saved capture |
| Developer trial | Four commands: `uv sync`, select a physical temporary Brain path, then capture and search through `uv run --frozen open-brain` with that explicit path | The checkout's executable retrieves its own saved capture |
| AI-client memory in current source | Select client, scope and individual grants; preview `agent setup`; inspect fragments; repeat with exact preview ID and `--apply`; restart and complete client trust/approval | A real client saves a fact and retrieves it in a new session |
| Obsidian | Set up the managed workspace; stage plugin files; open the sibling vault; enable the community plugin | The plugin runs against the selected Brain; staged assets alone do not prove activation |

macOS uses `$HOME/Library/Application Support/open-brain/brain`. Linux uses
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`. An absolute `--data-dir`
is an expert/test override. The core has no user-edited startup configuration
file. A temporary demo Brain is separate from this persistent default and is not
a suitable unannounced destination for lasting agent memory.

## Findings

1. **Published installation and source documentation describe different products.**
   The public [tap formula](https://github.com/cbolden15/homebrew-tap/blob/main/Formula/open-brain.rb)
   selects the [v0.1.0 release](https://github.com/cbolden15/open-brain/releases/tag/v0.1.0),
   published September 13. The observed installed help has no `agent`, `catalog`
   or general `setup` command. The v0.2.0 release is a draft. Current source also
   uses package version 0.1.0, so the version string does not establish feature
   availability. The published formula has base and Graphify resources; current
   source installation text additionally describes newer embedding and layout
   behavior. Release and source instructions need explicit boundaries.

2. **The first-use link leads to a historical executable fixture.**
   The baseline install guide recommends the executable examples in
   `docs/first-use.md`. Those examples deliberately require coordinates 10/5/5
   and reject the current 23/16/18 runtime. They need to remain historical test
   evidence. The page needs a current beginner entry before that material.

3. **There is no discoverable guided route through the existing services.**
   Bare invocation gives a parse failure. `init` prints storage metadata, and
   capture prints an identity, without explaining the next useful action. Agent
   setup requires assembling flags, interpreting JSON and repeating a command.
   A first-run coordinator can ask the few choices that matter while reusing
   those existing service contracts.

4. **Optional integrations appear entangled with basic use.**
   The synthetic README trial uses a temporary Brain and carries it into an
   optional agent preview. Persistent storage appears later. Obsidian adds two
   CLI operations and manual activation. These are reasonable optional steps,
   but neither is necessary to prove local memory works. Developer instructions
   should show that first result before the full contributor verification gate.

5. **Several onboarding claims are stale.**
   README coordinates say 21/16/16, agent setup also says 17/12/12, and references
   to an available desktop route conflict with its archived status. Hardcoded
   grant counts have drifted. These contradictions require users to decide
   which instructions to trust before they can finish setup.

## Implementation decision

| Approach | Tradeoff and decision |
|---|---|
| Current beginner guide with existing commands | Useful immediately, including for published v0.1.0; does not provide the requested guided configuration choices |
| Shared first-run coordinator with a terminal interface | Chosen. Compose the existing local capture/search, agent preview/apply and workspace/plugin services; keep one implementation of permissions and file safety |
| Persisted onboarding state machine shared by multiple interfaces | Sound larger architecture if another supported interface needs resumable setup. There is currently one terminal consumer; a new persistent workflow and migration are unnecessary |

The new source-candidate entry point is `open-brain setup`. It must not be
advertised as available from the existing public tap until a matching release
ships. Source users invoke it as `uv run --frozen open-brain setup`.

The sequence is:

1. Require an interactive terminal before creating state. Show the chosen Brain
   location and explain that local capture/search need no provider. Obtain
   confirmation before writing the displayed fixed example.
2. Capture the example as Personal data with a stable delivery ID. Verify its
   exact capture identity in search. A queued receipt is not a successful save;
   failure or interruption must preserve truthful progress.
3. Offer AI-client setup with skip as the default. Ask about capture and search
   separately, both default off. Display the existing configuration preview and
   require approval before applying its exact preview ID. Never infer broader
   permissions, replace unrelated settings or claim the client is connected.
4. Offer Obsidian setup with skip as the default. Reuse workspace and packaged
   plugin checks, show the vault location, and explain manual activation. Do not
   launch a client, configure a provider or enable collection automatically.
5. Report what actually completed and give a usable next command. Existing
   headless operations remain the automation route; the interactive command
   must reject redirected input and JSON mode before mutation.

## Evidence and limits

The installed v0.1.0 executable saved and retrieved the same synthetic capture
in a fresh temporary Brain on macOS arm64. The two commands took 11.73 seconds
together on this host. This excludes installation and is a single diagnostic
observation, not a cold-start or five-minute acceptance claim. No real Brain or
client profile was accessed. A first attempt using an unresolved macOS `/var`
temporary path was refused; resolving the path before passing `--data-dir`
succeeded, as the existing README demo already does.

The source executable also saved and retrieved its exact capture identity in a
separate synthetic Brain. Its two `uv run --frozen` commands took 1.86 seconds
after the environment was installed. These observations use different runtime
packaging and cache conditions; they are not a comparative startup benchmark.

The historical documentation has eleven marked blocks. Their bodies and pins
must remain untouched. Its existing `mcp-grant-isolation` body/pin mismatch is a
baseline issue, not a reason to present the historical fixture as current
onboarding or to silently rewrite its certification evidence.

Implementation verification must cover terminal refusal before mutation,
cancellation, repeat-run idempotency, capture custody and exact retrieval,
default-off grants, preview binding and optional setup failures. Actual client
trust and Obsidian GUI activation remain separate checks. This work does not
publish a release or change the Homebrew formula.

Independent review found two retry-path issues in the first implementation.
Headless guidance now retains an explicitly selected Brain in both suggested
commands. Agent setup started through `python -m open_brain` now resolves the
console script in that same Python environment, rather than treating the
non-executable module file as the MCP executable. Both have regression tests;
the module-mode flow also completed in a real synthetic terminal against an
existing disposable Codex configuration. Its unchanged result was reported as
configuration, not proof of a live client connection.

The candidate also passed the native distribution audit and Homebrew smoke
journey on macOS arm64, preserving the existing installed product. These checks
do not publish the candidate or establish actual client trust or Obsidian GUI
activation.
