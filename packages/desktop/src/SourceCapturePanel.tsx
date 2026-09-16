import { useCallback, useEffect, useMemo, useState } from "react";
import { errorMessage, request } from "./client";
import "./SourceCapturePanel.css";

type Provider = "gmail" | "google_drive" | "slack";
type Account = { provider: Provider; connection_id: string; display_name: string; scopes: string[]; expires_at_epoch: number | null };
type Resource = { resource_id: string; name: string; resource_type: string };
type Selection = { connector_name: string; connection_id: string; resource_id: string; resource_type: string };
type SourceStatus = {
  source_id: string;
  selection: Selection;
  options: Record<string, unknown>;
  status: "disabled" | "enabled" | "paused";
  interval_seconds: number;
  next_run_epoch: number | null;
  pause_ack_epoch: number | null;
  last_run: null | { outcome: string; captured_count?: number; duplicate_count?: number; failure_code?: string; finished_epoch: number; notices?: string[]; has_more?: boolean };
};
type SourceSnapshot = { schema_version: 1; sources: SourceStatus[]; collector: { background: boolean; connected: boolean } };
type SourcePreview = { preview_id: string; source_id: string; count: number; has_more: boolean; notices: string[]; records: { title: string; source_reference: string; trust: "unverified" }[] };
type SessionPreview = { preview_id: string; client: "claude_code" | "codex"; project_path: string; changes: string[]; capture_summary: boolean; capture_transcript: boolean; action: "configure" | "remove" };

type ProviderDefinition = {
  label: string;
  connector: string;
  resourceLabel: string;
  selectionLabel: string;
  scopeDisclosure: string;
  needsDateFloor: boolean;
};

const providers: Record<Provider, ProviderDefinition> = {
  gmail: {
    label: "Gmail", connector: "gmail", resourceLabel: "labels", selectionLabel: "label",
    scopeDisclosure: "Gmail authorization reads all mail in this Google account. Open Brain imports only the label you select below.", needsDateFloor: true,
  },
  google_drive: {
    label: "Google Drive", connector: "google_drive", resourceLabel: "files", selectionLabel: "file",
    scopeDisclosure: "Google Drive authorization reads all files in this Google account. Open Brain imports only the file you select below.", needsDateFloor: false,
  },
  slack: {
    label: "Slack", connector: "slack", resourceLabel: "channels", selectionLabel: "channel",
    scopeDisclosure: "Slack authorization reads public and private channels you can access. Open Brain imports only the channel you select below.", needsDateFloor: true,
  },
};

const providerList: Provider[] = ["gmail", "google_drive", "slack"];
const emptyAccounts: Record<Provider, Account[]> = { gmail: [], google_drive: [], slack: [] };
const emptyResources: Record<Provider, Resource[]> = { gmail: [], google_drive: [], slack: [] };
const emptyCursors: Record<Provider, string | null> = { gmail: null, google_drive: null, slack: null };
const emptySelections: Record<Provider, { connectionId: string; resourceId: string; dateFloor: string }> = {
  gmail: { connectionId: "", resourceId: "", dateFloor: sevenDaysAgo() },
  google_drive: { connectionId: "", resourceId: "", dateFloor: sevenDaysAgo() },
  slack: { connectionId: "", resourceId: "", dateFloor: sevenDaysAgo() },
};

function sevenDaysAgo(): string {
  const date = new Date();
  date.setDate(date.getDate() - 7);
  return date.toISOString().slice(0, 10);
}

function sourceError(error: unknown): string {
  const messages: Record<string, string> = {
    account_not_connected: "Connect an account before choosing a resource.",
    oauth_cancelled: "Account connection was cancelled. You can try again when ready.",
    oauth_failed: "The account connection did not complete. Check the provider setup and try again.",
    account_access_revoked: "This account no longer grants access. Disconnect it, then connect it again.",
    source_selection_invalid: "Choose an account and one available resource before continuing.",
    source_preview_stale: "The selection changed after this preview. Save the selection and preview again.",
    source_not_found: "This saved source is no longer available. Refresh its status and choose it again.",
    invalid_source_options: "Check the selected date and try again.",
    collector_not_connected: "The optional collector is not connected. Start it, then refresh this page.",
    collector_unavailable: "Install the optional Open Brain collector, then refresh this page.",
    source_stale_preview: "The source changed after this preview. Prepare a new preview before importing.",
    source_capture_consent_required: "Choose summary capture, transcript capture, or both before configuring hooks.",
    source_busy: "This source is already running. Wait for its current import, then try again.",
    rate_limited: "The provider requested a pause. Scheduled collection will retry after that pause.",
  };
  return messages[String(error)] ?? errorMessage(error);
}

function slackDateFloor(value: string): string {
  return new Date(value + "T00:00:00.000Z").toISOString();
}

function accountFor(accounts: Account[], id: string): Account | undefined {
  return accounts.find(account => account.connection_id === id);
}

export function SourceCapturePanel({ disabled = false }: { disabled?: boolean }) {
  const [snapshot, setSnapshot] = useState<SourceSnapshot | null>(null);
  const [accounts, setAccounts] = useState<Record<Provider, Account[]>>(emptyAccounts);
  const [resources, setResources] = useState<Record<Provider, Resource[]>>(emptyResources);
  const [cursors, setCursors] = useState<Record<Provider, string | null>>(emptyCursors);
  const [selections, setSelections] = useState(emptySelections);
  const [clientConfigs, setClientConfigs] = useState<Record<Provider, string>>({ gmail: "", google_drive: "", slack: "" });
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<SourcePreview | null>(null);
  const [importResult, setImportResult] = useState<{ sourceId: string; captured: number; duplicates: number; hasMore: boolean; notices: string[] } | null>(null);
  const [scheduleDrafts, setScheduleDrafts] = useState<Record<string, string>>({});
  const [session, setSession] = useState<{ client: "claude_code" | "codex"; projectPath: string; captureSummary: boolean; captureTranscript: boolean; action: "configure" | "remove" }>({ client: "claude_code", projectPath: "", captureSummary: false, captureTranscript: false, action: "configure" });
  const [sessionPreview, setSessionPreview] = useState<SessionPreview | null>(null);
  const [sessionResult, setSessionResult] = useState("");

  const unavailable = disabled || pending !== null;
  const sourceBySelection = useMemo(() => new Map((snapshot?.sources ?? []).map(source => [
    source.selection.connector_name + "\u0000" + source.selection.connection_id + "\u0000" + source.selection.resource_id,
    source,
  ])), [snapshot]);

  const refreshStatus = useCallback(async () => {
    if (disabled) return;
    setPending("status");
    setError("");
    try { setSnapshot(await request<SourceSnapshot>("sources.status")); }
    catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }, [disabled]);

  useEffect(() => { void refreshStatus(); }, [refreshStatus]);

  function updateSelection(provider: Provider, patch: Partial<(typeof selections)[Provider]>) {
    setSelections(current => ({ ...current, [provider]: { ...current[provider], ...patch } }));
    setPreview(null);
    setImportResult(null);
    setError("");
  }
  function upsertSource(next: SourceStatus) {
    setSnapshot(current => current ? { ...current, sources: [...current.sources.filter(source => source.source_id !== next.source_id), next] } : current);
  }
  function sourceFor(provider: Provider): SourceStatus | undefined {
    const selection = selections[provider];
    return sourceBySelection.get(providers[provider].connector + "\u0000" + selection.connectionId + "\u0000" + selection.resourceId);
  }
  function selectionFor(provider: Provider): Selection | null {
    const selection = selections[provider];
    const resource = resources[provider].find(item => item.resource_id === selection.resourceId);
    if (!selection.connectionId || !resource) return null;
    return { connector_name: providers[provider].connector, connection_id: selection.connectionId, resource_id: resource.resource_id, resource_type: resource.resource_type };
  }
  async function loadAccounts(provider: Provider) {
    if (unavailable) return;
    setPending("accounts-" + provider);
    setError("");
    try {
      const result = await request<{ accounts: Account[] }>("sources.accounts", { provider });
      setAccounts(current => ({ ...current, [provider]: result.accounts }));
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function connect(provider: Provider) {
    const clientConfig = clientConfigs[provider].trim();
    if (unavailable || !clientConfig.startsWith("/")) {
      setError("Enter the absolute path to this provider's developer-client identity file. Open Brain never asks for a token.");
      return;
    }
    setPending("connect-" + provider);
    setError("");
    try {
      const result = await request<{ account: Account }>("sources.connect", { provider, client_config: clientConfig });
      setAccounts(current => ({ ...current, [provider]: [...current[provider].filter(account => account.connection_id !== result.account.connection_id), result.account] }));
      updateSelection(provider, { connectionId: result.account.connection_id, resourceId: "" });
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function disconnect(provider: Provider, connectionId: string) {
    if (unavailable) return;
    setPending("disconnect-" + provider);
    setError("");
    try {
      await request("sources.disconnect", { provider, connection_id: connectionId });
      setAccounts(current => ({ ...current, [provider]: current[provider].filter(account => account.connection_id !== connectionId) }));
      if (selections[provider].connectionId === connectionId) updateSelection(provider, { connectionId: "", resourceId: "" });
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function loadResources(provider: Provider, more = false) {
    const connectionId = selections[provider].connectionId;
    if (unavailable || !connectionId) return;
    setPending("resources-" + provider);
    setError("");
    try {
      const cursor = more ? cursors[provider] : null;
      const result = await request<{ resources: Resource[]; next_cursor: string | null }>("sources.resources", {
        provider, connection_id: connectionId, ...(cursor ? { cursor } : {}),
      });
      setResources(current => ({ ...current, [provider]: more ? [...current[provider], ...result.resources] : result.resources }));
      setCursors(current => ({ ...current, [provider]: result.next_cursor }));
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function configure(provider: Provider) {
    const selection = selectionFor(provider);
    if (unavailable || !selection) return;
    setPending("configure-" + provider);
    setError("");
    setPreview(null);
    setImportResult(null);
    try {
      const existing = sourceFor(provider);
      const definition = providers[provider];
      const result = await request<SourceStatus>("sources.configure", {
        ...(existing ? { source_id: existing.source_id, reset: true } : {}), selection,
        options: definition.needsDateFloor ? {
          date_floor: provider === "slack" ? slackDateFloor(selections[provider].dateFloor) : selections[provider].dateFloor,
        } : {},
      });
      upsertSource(result);
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function previewSource(sourceId: string) {
    if (unavailable) return;
    setPending("preview-" + sourceId);
    setError("");
    setImportResult(null);
    try { setPreview(await request<SourcePreview>("sources.preview", { source_id: sourceId })); }
    catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function importSource() {
    if (unavailable || !preview) return;
    setPending("import-" + preview.source_id);
    setError("");
    try {
      const result = await request<{ source_id: string; outcome: "completed"; captured_count: number; duplicate_count: number; has_more: boolean; notices: string[] }>("sources.import", { source_id: preview.source_id, preview_id: preview.preview_id });
      setImportResult({ sourceId: result.source_id, captured: result.captured_count, duplicates: result.duplicate_count, hasMore: result.has_more, notices: result.notices });
      setPreview(null);
      await refreshStatus();
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function control(sourceId: string, action: "enable" | "pause" | "resume" | "disable" | "schedule" | "sync_now", interval?: number) {
    if (unavailable) return;
    setPending("control-" + sourceId);
    setError("");
    try {
      const result = await request<SourceStatus>("sources.control", { source_id: sourceId, action, ...(interval ? { interval_seconds: interval } : {}) });
      upsertSource(result);
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function setBackground(enable: boolean) {
    if (unavailable) return;
    setPending("background");
    setError("");
    try {
      await request(enable ? "sources.background_enable" : "sources.background_disable");
      await refreshStatus();
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function previewSession() {
    if (unavailable || !session.projectPath.trim().startsWith("/")) {
      setError("Enter the absolute path to the project where this client should capture sessions.");
      return;
    }
    setPending("session-preview");
    setError("");
    setSessionResult("");
    try {
      setSessionPreview(await request<SessionPreview>("sources.session_preview", {
        client: session.client, project_path: session.projectPath.trim(), capture_summary: session.captureSummary,
        capture_transcript: session.captureTranscript, action: session.action,
      }));
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }
  async function applySession() {
    if (unavailable || !sessionPreview) return;
    setPending("session-apply");
    setError("");
    try {
      const result = await request<{ status: "configured" | "removed"; source_id: string }>("sources.session_apply", { preview_id: sessionPreview.preview_id });
      setSessionResult(result.status === "configured" ? "Session capture hooks were configured. Automatic capture remains disabled until you explicitly enable it." : "The owned session capture hooks were removed.");
      setSessionPreview(null);
      await refreshStatus();
    } catch (cause) { setError(sourceError(cause)); }
    finally { setPending(null); }
  }

  return <section className="source-capture-panel" aria-labelledby="source-capture-title">
    <div className="page-title"><p className="eyebrow">Sources</p><h1 id="source-capture-title">Choose what Open Brain can import.</h1><p>Connections grant provider access. You choose exactly what Open Brain captures.</p></div>
    {error ? <div className="notice error" role="alert">{error}</div> : null}
    {snapshot === null && !error ? <p className="notice" role="status">Checking optional collector status…</p> : null}
    <div className="source-capture-actions"><button type="button" className="secondary" disabled={unavailable} onClick={() => void refreshStatus()}>Refresh source status</button>{snapshot ? <span>{snapshot.collector.connected ? "Collector connected" : "Collector unavailable"}</span> : null}</div>

    <div className="source-provider-list">
      {providerList.map(provider => {
        const definition = providers[provider];
        const selection = selections[provider];
        const selectedAccount = accountFor(accounts[provider], selection.connectionId);
        const selectedSource = sourceFor(provider);
        const resourceReady = Boolean(selectionFor(provider));
        return <article className="source-provider" key={provider} aria-labelledby={provider + "-title"}>
          <div className="source-provider-heading"><div><h2 id={provider + "-title"}>{definition.label}</h2><p>{definition.scopeDisclosure}</p></div></div>
          <fieldset disabled={unavailable}>
            <label className="source-capture-field" htmlFor={provider + "-client-config"}>Developer-client identity file
              <input id={provider + "-client-config"} value={clientConfigs[provider]} onChange={event => setClientConfigs(current => ({ ...current, [provider]: event.target.value }))} placeholder="/absolute/path/to/developer-client.json" spellCheck={false} autoCapitalize="none" />
            </label>
            <p className="source-capture-help">A developer-client identity file is currently required before connection. Open Brain opens the provider's system browser and never asks for an access token. The browser step can take up to 3 minutes.</p>
            <div className="source-capture-button-row"><button type="button" onClick={() => void connect(provider)}>{pending === "connect-" + provider ? "Waiting for browser…" : "Connect " + definition.label}</button><button type="button" className="secondary" onClick={() => void loadAccounts(provider)}>{pending === "accounts-" + provider ? "Loading accounts…" : "Refresh accounts"}</button></div>
            <label className="source-capture-field" htmlFor={provider + "-account"}>Connected account
              <select id={provider + "-account"} value={selection.connectionId} onChange={event => { updateSelection(provider, { connectionId: event.target.value, resourceId: "" }); setResources(current => ({ ...current, [provider]: [] })); setCursors(current => ({ ...current, [provider]: null })); }}>
                <option value="">Choose an account</option>{accounts[provider].map(account => <option value={account.connection_id} key={account.connection_id}>{account.display_name}</option>)}
              </select>
            </label>
            {selectedAccount ? <div className="source-account-detail"><span>Connected as {selectedAccount.display_name}</span><button type="button" className="text-button" onClick={() => void disconnect(provider, selectedAccount.connection_id)}>Disconnect</button></div> : null}
            <div className="source-capture-button-row"><button type="button" className="secondary" disabled={!selection.connectionId} onClick={() => void loadResources(provider)}>{pending === "resources-" + provider ? "Loading " + definition.resourceLabel + "…" : "Load " + definition.resourceLabel}</button>{cursors[provider] ? <button type="button" className="text-button" onClick={() => void loadResources(provider, true)}>Load more {definition.resourceLabel}</button> : null}</div>
            <label className="source-capture-field" htmlFor={provider + "-resource"}>One {definition.selectionLabel} to import
              <select id={provider + "-resource"} value={selection.resourceId} onChange={event => updateSelection(provider, { resourceId: event.target.value })} disabled={!selection.connectionId || resources[provider].length === 0}>
                <option value="">Choose one {definition.selectionLabel}</option>{resources[provider].map(resource => <option key={resource.resource_id} value={resource.resource_id}>{resource.name}</option>)}
              </select>
            </label>
            {definition.needsDateFloor ? <label className="source-capture-field" htmlFor={provider + "-date-floor"}>Import from
              <input id={provider + "-date-floor"} type="date" value={selection.dateFloor} onChange={event => updateSelection(provider, { dateFloor: event.target.value })} />
            </label> : null}
            <div className="source-capture-button-row"><button type="button" disabled={!resourceReady} onClick={() => void configure(provider)}>{pending === "configure-" + provider ? "Saving selection…" : selectedSource ? "Update selection" : "Save selection"}</button>{selectedSource ? <button type="button" className="secondary" onClick={() => void previewSource(selectedSource.source_id)}>{pending === "preview-" + selectedSource.source_id ? "Preparing preview…" : "Preview import"}</button> : null}</div>
          </fieldset>
        </article>;
      })}
    </div>

    {preview ? <section className="source-preview" aria-labelledby="source-preview-title"><div className="section-label"><h2 id="source-preview-title">Review import</h2><span>{preview.count} record{preview.count === 1 ? "" : "s"}</span></div><p className="source-capture-help">Preview content is unverified source material. Nothing has been imported yet.</p><ul>{preview.records.map((record, index) => <li key={record.source_reference + index}><strong>{record.title}</strong><span>{record.source_reference}</span><small>Unverified source</small></li>)}</ul>{preview.notices.map(notice => <p className="source-capture-help" key={notice}>{notice}</p>)}{preview.has_more ? <p className="source-capture-help">This preview has more records. Importing this batch does not claim that later records are complete.</p> : null}<button type="button" disabled={unavailable} onClick={() => void importSource()}>{pending === "import-" + preview.source_id ? "Importing…" : "Import preview"}</button></section> : null}
    {importResult ? <div className="notice success" role="status"><p><strong>Import completed.</strong> {importResult.captured} captured, {importResult.duplicates} already present.</p>{importResult.hasMore ? <p>More records remain available. Review the next preview before importing again.</p> : null}{importResult.notices.map(notice => <p key={notice}>{notice}</p>)}</div> : null}

    <section className="source-configured" aria-labelledby="source-configured-title"><h2 id="source-configured-title">Saved sources</h2>{snapshot?.sources.length ? <div className="source-list">{snapshot.sources.map(source => <article className="source-row source-capture-saved" key={source.source_id}><div><h3>{source.selection.connector_name}: {source.selection.resource_id}</h3><p>{source.status}. {source.last_run ? source.last_run.outcome : "No import run yet."}</p>{source.status === "paused" && source.pause_ack_epoch === null ? <p className="source-capture-help">Pause was requested and is waiting for collector acknowledgement.</p> : null}</div><div className="source-capture-controls"><label htmlFor={"schedule-" + source.source_id}>Every seconds<input id={"schedule-" + source.source_id} type="number" min="60" value={scheduleDrafts[source.source_id] ?? String(source.interval_seconds)} onChange={event => setScheduleDrafts(current => ({ ...current, [source.source_id]: event.target.value }))} /></label><button type="button" className="secondary" disabled={unavailable || !Number.isInteger(Number(scheduleDrafts[source.source_id] ?? source.interval_seconds)) || Number(scheduleDrafts[source.source_id] ?? source.interval_seconds) < 60} onClick={() => void control(source.source_id, "schedule", Number(scheduleDrafts[source.source_id] ?? source.interval_seconds))}>Save schedule</button><button type="button" disabled={unavailable} onClick={() => void control(source.source_id, source.status === "enabled" ? "pause" : source.status === "paused" ? "resume" : "enable")}>{source.status === "enabled" ? "Pause" : source.status === "paused" ? "Resume" : "Enable automatic capture"}</button><button type="button" className="secondary" disabled={unavailable} onClick={() => void control(source.source_id, "sync_now")}>Sync now</button><button type="button" className="text-button" disabled={unavailable} onClick={() => void control(source.source_id, "disable")}>Disable source</button></div></article>)}</div> : <p className="helper-text">No source selections are saved yet. Automatic capture starts off.</p>}</section>

    {snapshot ? <section className="source-background" aria-labelledby="source-background-title"><h2 id="source-background-title">Keep collecting after the desktop closes</h2><p>{snapshot.collector.background ? "Background collection is on. Stopping it returns collection to this desktop session." : "Background collection is off. Enable the optional collector only if you want already-enabled sources to run after closing this window."}</p><button type="button" disabled={unavailable || !snapshot.collector.connected} onClick={() => void setBackground(!snapshot.collector.background)}>{pending === "background" ? "Updating background collector…" : snapshot.collector.background ? "Stop background collector" : "Enable background collector"}</button></section> : null}

    <section className="source-sessions" aria-labelledby="source-sessions-title"><h2 id="source-sessions-title">Agent session capture</h2><p>Select a project and explicitly choose what its Claude Code or Codex hooks may capture. Summaries and transcripts both start off.</p><fieldset disabled={unavailable}><div className="field-grid"><label>Client<select value={session.client} onChange={event => { setSession(current => ({ ...current, client: event.target.value as "claude_code" | "codex" })); setSessionPreview(null); }}><option value="claude_code">Claude Code</option><option value="codex">Codex</option></select></label><label>Action<select value={session.action} onChange={event => { setSession(current => ({ ...current, action: event.target.value as "configure" | "remove" })); setSessionPreview(null); }}><option value="configure">Configure hooks</option><option value="remove">Remove owned hooks</option></select></label></div><label className="block-field">Project directory<input value={session.projectPath} onChange={event => { setSession(current => ({ ...current, projectPath: event.target.value })); setSessionPreview(null); }} placeholder="/absolute/path/to/project" spellCheck={false} autoCapitalize="none" /></label><div className="permission-options"><label><input type="checkbox" checked={session.captureSummary} onChange={event => { setSession(current => ({ ...current, captureSummary: event.target.checked })); setSessionPreview(null); }} /><span><strong>Capture extractive session summaries</strong><small>Off by default. Any captured summary is extractive, not model-generated.</small></span></label><label><input type="checkbox" checked={session.captureTranscript} onChange={event => { setSession(current => ({ ...current, captureTranscript: event.target.checked })); setSessionPreview(null); }} /><span><strong>Capture permitted transcript text</strong><small>Off by default. Tool output, hidden reasoning, Brain-returned content, and detected secrets are excluded.</small></span></label></div><button type="button" onClick={() => void previewSession()}>{pending === "session-preview" ? "Preparing preview…" : session.action === "remove" ? "Preview hook removal" : "Preview session hooks"}</button></fieldset>{sessionPreview ? <div className="source-session-preview"><h3>{sessionPreview.action === "remove" ? "Review hook removal" : "Review session hooks"}</h3><p>Nothing has changed yet.</p><ul>{sessionPreview.changes.map((change, index) => <li key={change + index}>{change}</li>)}</ul><button type="button" disabled={unavailable} onClick={() => void applySession()}>{pending === "session-apply" ? "Applying…" : sessionPreview.action === "remove" ? "Apply removal" : "Apply hooks"}</button></div> : null}{sessionResult ? <div className="notice success" role="status">{sessionResult}</div> : null}</section>
  </section>;
}
