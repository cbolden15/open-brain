# First use

This guide exercises the supported core 0.1.0 journey with synthetic data. It uses the
foreground CLI only. `OPEN_BRAIN` may name an installed `open-brain`; the documentation gate sets
it to the exact native candidate. The temporary Brain and managed vault are removed when you
remove `RUN_ROOT`.

The runnable blocks are one sequence. The executable verification requires `jq` as an explicit
prerequisite. Open Brain itself remains offline for every operation below.

<!-- open-brain-example:first-use-environment -->
```sh
set -eu
OPEN_BRAIN="${OPEN_BRAIN:-$(command -v open-brain)}"
RUN_ROOT="${RUN_ROOT:-$(mktemp -d "${TMPDIR:-/tmp}/open-brain-first-use.XXXXXX")}"
export OPEN_BRAIN RUN_ROOT
DATA_DIR="$RUN_ROOT/brain"
IMPORT_DIR="$RUN_ROOT/import"
mkdir -p "$IMPORT_DIR" "$RUN_ROOT/claude-project" "$RUN_ROOT/codex-project"
printf '%s\n' '# Aurora rendezvous' '' 'Imported café 🚀 evidence, second source.' > "$IMPORT_DIR/aurora.md"
CATALOG="$("$OPEN_BRAIN" catalog --schema-version 2 --json)"
test ! -e "$DATA_DIR"
test "$(printf '%s' "$CATALOG" | jq -r '.schema_version')" = 2
test "$(printf '%s' "$CATALOG" | jq -r '.product.public_acceptance')" = not_certified
test "$(printf '%s' "$CATALOG" | jq -r '.compatibility.state_schema')" = 7
test "$(printf '%s' "$CATALOG" | jq -r '.compatibility.runtime_session')" = 2
test "$(printf '%s' "$CATALOG" | jq -r '.compatibility.task_contract')" = t03.v1
test "$(printf '%s' "$CATALOG" | jq -r '.compatibility.portable_metadata')" = 4
test "$(printf '%s' "$CATALOG" | jq -r '.acceptance.trusted_certifications | length')" = 0
"$OPEN_BRAIN" --help | grep -q 'workspace'
"$OPEN_BRAIN" mcp --help | grep -q -- '--allow-review-decide'
if "$OPEN_BRAIN" vault path >/dev/null 2>&1; then exit 1; fi
INIT="$("$OPEN_BRAIN" init --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$INIT" | jq -r '.status')" = initialized
```

Capture one owner-authored note, import one Markdown file, and derive both immutable capture IDs
from actual command output.

<!-- open-brain-example:capture-and-import -->
```sh
CAPTURE="$("$OPEN_BRAIN" capture 'Aurora rendezvous owner note with naïve café coordinates and continuation marker αβγ.' --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$CAPTURE" | jq -r '.status')" = captured
OWNER_CAPTURE_ID="$(printf '%s' "$CAPTURE" | jq -er '.capture_id')"
IMPORT="$("$OPEN_BRAIN" import "$IMPORT_DIR" --data-dir "$DATA_DIR" --yes --json)"
test "$(printf '%s' "$IMPORT" | jq -r '.status')" = completed
test "$(printf '%s' "$IMPORT" | jq -r '.imported')" = 1
INBOX="$("$OPEN_BRAIN" inbox list --data-dir "$DATA_DIR" --json)"
IMPORTED_CAPTURE_ID="$(printf '%s' "$INBOX" | jq -er '.items[] | select(.payload_family == "reference_or_file") | .capture_id')"
test "$OWNER_CAPTURE_ID" != "$IMPORTED_CAPTURE_ID"
```

Routing assigns both sources to a space; it does not publish. Publication requires a proposal, a
complete inspection, and approval with the review token returned by that inspection. This
synthetic walkthrough approves automatically only after byte-for-byte assertions. With real notes,
read the complete `review show` output yourself before running `review approve`.

<!-- open-brain-example:route-review-publish -->
```sh
SPACE="$("$OPEN_BRAIN" space create 'First use' --data-dir "$DATA_DIR" --json)"
SPACE_ID="$(printf '%s' "$SPACE" | jq -er '.space.space_id')"
for CAPTURE_ID in "$OWNER_CAPTURE_ID" "$IMPORTED_CAPTURE_ID"; do
  ROUTED="$("$OPEN_BRAIN" inbox route "$CAPTURE_ID" "$SPACE_ID" --data-dir "$DATA_DIR" --json)"
  test "$(printf '%s' "$ROUTED" | jq -r '.status')" = routed
done
DRAFT="$RUN_ROOT/aurora-draft.md"
printf '%s\n' '# Aurora rendezvous' '' 'Combined naïve café coordinates αβγ with imported 🚀 evidence.' '' > "$DRAFT"
LINE=1
while test "$LINE" -le 48; do
  printf 'Continuation segment %02d: morning noon evening — café 🚀 αβγ.\n' "$LINE" >> "$DRAFT"
  LINE=$((LINE + 1))
done
printf '%s' 'Final Unicode sentinel: Zażółć gęślą jaźń 🌌.' >> "$DRAFT"
PROPOSAL="$("$OPEN_BRAIN" review propose --capture-id "$OWNER_CAPTURE_ID" --capture-id "$IMPORTED_CAPTURE_ID" --title 'Aurora rendezvous' --markdown-file "$DRAFT" --data-dir "$DATA_DIR" --json)"
PROPOSAL_ID="$(printf '%s' "$PROPOSAL" | jq -er '.proposal_id')"
SHOWN="$("$OPEN_BRAIN" review show "$PROPOSAL_ID" --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$SHOWN" | jq -r '.status')" = shown
test "$(printf '%s' "$SHOWN" | jq -r '.capture_ids | length')" = 2
test "$(printf '%s' "$SHOWN" | jq -r --arg first "$OWNER_CAPTURE_ID" --arg second "$IMPORTED_CAPTURE_ID" '(.capture_ids | index($first)) != null and (.capture_ids | index($second)) != null')" = true
test "$(printf '%s' "$SHOWN" | jq -r '.markdown | contains("Zażółć gęślą jaźń 🌌")')" = true
INSPECTED="$RUN_ROOT/inspected.md"
printf '%s' "$SHOWN" | jq -rj '.markdown' > "$INSPECTED"
cmp "$DRAFT" "$INSPECTED"
REVIEW_TOKEN="$(printf '%s' "$SHOWN" | jq -er '.review_token')"
APPROVED="$("$OPEN_BRAIN" review approve "$PROPOSAL_ID" --review-token "$REVIEW_TOKEN" --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$APPROVED" | jq -r '.status')" = approved
PAGE_ID="$(printf '%s' "$APPROVED" | jq -er '.page_id')"
```

`workspace setup` creates the managed vault at `Open Brain Vault` beside the Brain directory. There
is no `vault` or `vault path` command. The receipt deliberately omits private paths.

<!-- open-brain-example:workspace-search-and-complete-read -->
```sh
WORKSPACE_SETUP="$("$OPEN_BRAIN" workspace setup --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$WORKSPACE_SETUP" | jq -r '.status')" = setup
WORKSPACE_STATUS="$("$OPEN_BRAIN" workspace status --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$WORKSPACE_STATUS" | jq -r '.connected')" = true
test "$(printf '%s' "$WORKSPACE_STATUS" | jq -r '.active_notes')" = 1
VAULT="$RUN_ROOT/Open Brain Vault"
test -d "$VAULT"
test "$(find "$VAULT" -type f -name '*.md' | wc -l | tr -d ' ')" = 1
grep -R -q 'Zażółć gęślą jaźń 🌌' "$VAULT"
CANONICAL_SEARCH="$("$OPEN_BRAIN" search-page 'Aurora rendezvous' --record-type canonical --limit 1 --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$CANONICAL_SEARCH" | jq -r '.results[0].record_id')" = "$PAGE_ID"
test "$(printf '%s' "$CANONICAL_SEARCH" | jq -r --arg first "$OWNER_CAPTURE_ID" --arg second "$IMPORTED_CAPTURE_ID" '(.results[0].provenance.capture_ids | index($first)) != null and (.results[0].provenance.capture_ids | index($second)) != null')" = true
REVISION_ID="$(printf '%s' "$CANONICAL_SEARCH" | jq -er '.results[0].revision_id')"
SOURCE_PAGE_ONE="$("$OPEN_BRAIN" search-page 'Aurora rendezvous' --record-type source --limit 1 --data-dir "$DATA_DIR" --json)"
SOURCE_CURSOR="$(printf '%s' "$SOURCE_PAGE_ONE" | jq -er '.next_cursor')"
SOURCE_PAGE_TWO="$("$OPEN_BRAIN" search-page 'Aurora rendezvous' --record-type source --limit 1 --cursor "$SOURCE_CURSOR" --data-dir "$DATA_DIR" --json)"
test "$(printf '%s\n%s' "$SOURCE_PAGE_ONE" "$SOURCE_PAGE_TWO" | jq -sr '[.[].results[].record_id] | unique | length')" = 2
test "$(printf '%s' "$SOURCE_PAGE_TWO" | jq -r '.complete')" = true
RECONSTRUCTED="$RUN_ROOT/reconstructed.md"
: > "$RECONSTRUCTED"
READ_CURSOR=''
READ_CHUNKS=0
PREVIOUS_END=0
while :; do
  if test -n "$READ_CURSOR"; then
    CHUNK="$("$OPEN_BRAIN" read "$PAGE_ID" --expected-revision-id "$REVISION_ID" --target-bytes 1024 --cursor "$READ_CURSOR" --data-dir "$DATA_DIR" --json)"
  else
    CHUNK="$("$OPEN_BRAIN" read "$PAGE_ID" --expected-revision-id "$REVISION_ID" --target-bytes 1024 --data-dir "$DATA_DIR" --json)"
  fi
  test "$(printf '%s' "$CHUNK" | jq -r '.content.kind')" = untrusted_text
  test "$(printf '%s' "$CHUNK" | jq -r '.start_byte')" = "$PREVIOUS_END"
  NEXT_END="$(printf '%s' "$CHUNK" | jq -er '.end_byte')"
  test "$NEXT_END" -gt "$PREVIOUS_END"
  PREVIOUS_END="$NEXT_END"
  printf '%s' "$CHUNK" | jq -rj '.content.text' >> "$RECONSTRUCTED"
  READ_CHUNKS=$((READ_CHUNKS + 1))
  test "$READ_CHUNKS" -le 16
  READ_CURSOR="$(printf '%s' "$CHUNK" | jq -r '.next_cursor // empty')"
  if test -n "$READ_CURSOR"; then
    test "$(printf '%s' "$CHUNK" | jq -r '.complete')" = false
  else
    test "$(printf '%s' "$CHUNK" | jq -r '.complete')" = true
    break
  fi
done
test "$READ_CHUNKS" -gt 1
EXPECTED_PROJECTED="$RUN_ROOT/expected-projected.md"
cp "$DRAFT" "$EXPECTED_PROJECTED"
printf '\n' >> "$EXPECTED_PROJECTED"
cmp "$EXPECTED_PROJECTED" "$RECONSTRUCTED"
VAULT_NOTE="$(find "$VAULT" -type f -name '*.md')"
VAULT_BODY="$RUN_ROOT/vault-body.md"
awk 'BEGIN { markers=0 } /^---$/ { markers++; next } markers >= 2 { print }' "$VAULT_NOTE" > "$VAULT_BODY"
EXPECTED_VAULT_BODY="$RUN_ROOT/expected-vault-body.md"
printf '\n' > "$EXPECTED_VAULT_BODY"
cat "$EXPECTED_PROJECTED" >> "$EXPECTED_VAULT_BODY"
cmp "$EXPECTED_VAULT_BODY" "$VAULT_BODY"
```

MCP grants are independent and default off. These are the exact capture-only, search-only, and
combined argument sets used by the agent configurations. The catalog tool is always present; the
search grant exposes paged search and contract discovery. The legacy owner search tool stays hidden
from this scoped session. The exchange checks catalog
authorization and proves a capture call is hidden from a search-only session.

<!-- open-brain-example:mcp-grant-isolation -->
```sh
mcp_exchange() {
  MCP_OUTPUT="$RUN_ROOT/mcp-$1.jsonl"
  shift
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"first-use","version":"1"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"brain_catalog","arguments":{"schema_version":2}}}' \
    '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"brain_capture","arguments":{"text":"must remain denied"}}}' \
    | "$OPEN_BRAIN" mcp --data-dir "$DATA_DIR" "$@" > "$MCP_OUTPUT"
}
mcp_exchange capture-only --allow-capture
test "$(jq -sr '.[1].result.tools | map(.name) | sort == ["brain_capture","brain_catalog"]' "$RUN_ROOT/mcp-capture-only.jsonl")" = true
mcp_exchange search-only --allow-search
test "$(jq -sr '.[1].result.tools | map(.name) | sort == ["brain_catalog","brain_contract_describe","brain_search_page"]' "$RUN_ROOT/mcp-search-only.jsonl")" = true
test "$(jq -sr '.[2].result.structuredContent.surfaces.mcp.authorized_tools == ["brain_catalog","brain_contract_describe","brain_search_page"]' "$RUN_ROOT/mcp-search-only.jsonl")" = true
test "$(jq -sr '.[3].result.isError and .[3].result.content[0].text == "unknown tool"' "$RUN_ROOT/mcp-search-only.jsonl")" = true
mcp_exchange combined --allow-capture --allow-search
test "$(jq -sr '.[1].result.tools | map(.name) | sort == ["brain_capture","brain_catalog","brain_contract_describe","brain_search_page"]' "$RUN_ROOT/mcp-combined.jsonl")" = true
if "$OPEN_BRAIN" mcp --data-dir "$DATA_DIR" </dev/null >/dev/null 2>&1; then exit 1; fi
```

Agent setup first previews the owned configuration fragments. Applying requires that exact preview
ID; removal follows the same preview/apply rule. The example uses disposable project profiles and
all nine default-off agent grants.

<!-- open-brain-example:agent-setup-lifecycle -->
```sh
agent_lifecycle() {
  CLIENT="$1"
  PROJECT="$2"
  EXPECTED_AGENT_ARGS="$(jq -cn --arg data "$DATA_DIR" '["mcp","--data-dir",$data,"--allow-capture","--allow-search","--allow-content-read","--allow-history-read","--allow-inbox-read","--allow-organize","--allow-review-read","--allow-review-propose","--allow-review-decide"]')"
  EXPECTED_AGENT_TOOLS='["brain_catalog","brain_capture","brain_search","brain_contract_describe","brain_search_page","brain_read","brain_history_list","brain_history_show","brain_inbox_list","brain_space_list","brain_space_create","brain_space_rename","brain_inbox_route","brain_source_route","brain_review_list","brain_review_show","brain_review_propose","brain_review_approve","brain_review_reject","brain_review_edit_and_approve"]'
  PREVIEW="$("$OPEN_BRAIN" agent setup --client "$CLIENT" --scope project --project-dir "$PROJECT" --allow-capture --allow-search --allow-content-read --allow-history-read --allow-inbox-read --allow-organize --allow-review-read --allow-review-propose --allow-review-decide --runtime "$OPEN_BRAIN" --data-dir "$DATA_DIR" --json)"
  PREVIEW_ID="$(printf '%s' "$PREVIEW" | jq -er '.preview_id')"
  test "$(printf '%s' "$PREVIEW" | jq -r '.status')" = preview
  APPLIED="$("$OPEN_BRAIN" agent setup --client "$CLIENT" --scope project --project-dir "$PROJECT" --allow-capture --allow-search --allow-content-read --allow-history-read --allow-inbox-read --allow-organize --allow-review-read --allow-review-propose --allow-review-decide --runtime "$OPEN_BRAIN" --data-dir "$DATA_DIR" --apply --preview-id "$PREVIEW_ID" --json)"
  test "$(printf '%s' "$APPLIED" | jq -r '.status')" = configured
  CONFIG_COPY="$RUN_ROOT/$CLIENT-config"
  if test "$CLIENT" = claude-code; then
    cp "$PROJECT/.mcp.json" "$CONFIG_COPY"
    CONFIG_COMMAND="$(jq -er '.mcpServers["open-brain"].command' "$CONFIG_COPY")"
    CONFIG_ARGS_JSON="$(jq -c '.mcpServers["open-brain"].args' "$CONFIG_COPY")"
    CONFIG_LINE="$(jq -r '.mcpServers["open-brain"] | ([.command] + .args) | map(@sh) | join(" ")' "$CONFIG_COPY")"
  else
    cp "$PROJECT/.codex/config.toml" "$CONFIG_COPY"
    CONFIG_COMMAND="$(awk -F ' = ' '$1 == "command" { print $2 }' "$CONFIG_COPY" | jq -r .)"
    CONFIG_ARGS_JSON="$(awk -F ' = ' '$1 == "args" { print $2 }' "$CONFIG_COPY" | jq -c .)"
    CONFIG_ARGS="$(printf '%s' "$CONFIG_ARGS_JSON" | jq -r '.[] | @sh' | tr '\n' ' ')"
    CONFIG_LINE="$(printf '%s' "$CONFIG_COMMAND" | jq -Rr @sh) $CONFIG_ARGS"
  fi
  test "$CONFIG_COMMAND" = "$OPEN_BRAIN"
  test "$CONFIG_ARGS_JSON" = "$EXPECTED_AGENT_ARGS"
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"configured-client","version":"1"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"brain_catalog","arguments":{"schema_version":2}}}' \
    | eval "$CONFIG_LINE" > "$RUN_ROOT/$CLIENT-configured.jsonl"
  test "$(jq -sr --argjson expected "$EXPECTED_AGENT_TOOLS" 'length == 3 and (.[0] | has("error") | not) and .[0].result.capabilities == {"tools":{}} and (.[1] | has("error") | not) and ([.[1].result.tools[].name] | sort) == ($expected | sort) and (.[2] | has("error") | not) and ((.[2].result.isError // false) == false) and (.[2].result.structuredContent.surfaces.mcp.authorized_tools | sort) == ($expected | sort)' "$RUN_ROOT/$CLIENT-configured.jsonl")" = true
  REMOVE_PREVIEW="$("$OPEN_BRAIN" agent setup --client "$CLIENT" --scope project --project-dir "$PROJECT" --action remove --runtime "$OPEN_BRAIN" --data-dir "$DATA_DIR" --json)"
  REMOVE_ID="$(printf '%s' "$REMOVE_PREVIEW" | jq -er '.preview_id')"
  REMOVED="$("$OPEN_BRAIN" agent setup --client "$CLIENT" --scope project --project-dir "$PROJECT" --action remove --runtime "$OPEN_BRAIN" --data-dir "$DATA_DIR" --apply --preview-id "$REMOVE_ID" --json)"
  test "$(printf '%s' "$REMOVED" | jq -r '.status')" = removed
}
agent_lifecycle claude-code "$RUN_ROOT/claude-project"
agent_lifecycle codex "$RUN_ROOT/codex-project"
```

The following synthetic verification installs, checks, and then removes the packaged Obsidian
assets from the disposable vault, so it deliberately leaves the plugin absent. For normal use,
follow the [installation guide](install.md), install into your actual managed vault, and then enable
the `Open Brain` community plugin manually in Obsidian. Asset presence and bridge checks do not
establish GUI acceptance.

<!-- open-brain-example:obsidian-plugin-lifecycle -->
```sh
PLUGIN_INSTALL="$("$OPEN_BRAIN" obsidian-plugin install --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$PLUGIN_INSTALL" | jq -r '.status')" = installed
PLUGIN_STATUS="$("$OPEN_BRAIN" obsidian-plugin status --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$PLUGIN_STATUS" | jq -r '.status')" = current
PLUGIN_REMOVE="$("$OPEN_BRAIN" obsidian-plugin remove --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$PLUGIN_REMOVE" | jq -r '.status')" = removed
```

Version 0.1.0 here identifies package compatibility, not public certification. Connected-source
refresh, lifecycle/forget operations, document extraction, semantic recall, provider readiness,
and a released desktop application remain outside this core journey.
