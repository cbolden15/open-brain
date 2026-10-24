import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { errorMessage, negotiatedOperations, request, saveMayHaveCompleted, setupReady, type BrainStatus, type CollectorEnableInput, type CollectorStatus, type SetupInput, type SetupPreview, type SetupResult } from "./client";
import { PublicationPanel } from "./PublicationPanel";
import { SearchPanel } from "./SearchPanel";
import { SourceCapturePanel } from "./SourceCapturePanel";
import { parseContractDescription } from "./t03-wire";

const destinations = ["Search", "Capture", "Review", "Sources", "Activity", "Settings"] as const;
type Destination = typeof destinations[number];
type Activity = { id: string; title: string; detail: string; at: Date; failed: boolean };

export function App() {
  const [page, setPage] = useState<Destination>("Search");
  const [brain, setBrain] = useState<BrainStatus | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [operations, setOperations] = useState<Set<string>>(new Set());
  const [draft, setDraft] = useState("");
  const [pendingSave, setPendingSave] = useState<{ id: string; text: string } | null>(null);
  const [saveError, setSaveError] = useState("");
  const [savedText, setSavedText] = useState("");
  const [activity, setActivity] = useState<Activity[]>([]);
  const [collector, setCollector] = useState<CollectorStatus | null>(null);
  const [collectorError, setCollectorError] = useState("");
  const [collectorSetup, setCollectorSetup] = useState<CollectorEnableInput>({
    source_id: "github.fixture.closed",
    connector_name: "github",
    connection_id: "account:fixture",
    credential_ref: "github-user-token:fixture",
    resource_id: "repo:fixture/open-brain",
    resource_type: "repository",
    interval_seconds: 900,
  });
  const [setup, setSetup] = useState<SetupInput>({
    client: "claude-code", scope: "project", project_dir: "",
    allow_capture: false, allow_search: false, action: "configure",
  });
  const [preview, setPreview] = useState<SetupPreview | null>(null);
  const [setupResult, setSetupResult] = useState<SetupResult | null>(null);
  const [setupError, setSetupError] = useState("");

  const record = useCallback((title: string, detail: string, failed = false) => {
    setActivity(previous => [{ id: crypto.randomUUID(), title, detail, failed, at: new Date() }, ...previous].slice(0, 100));
  }, []);
  const connect = useCallback(async (restart = false) => {
    setConnecting(true);
    setConnectionError("");
    try {
      if (restart) await invoke("desktop_reconnect");
      const status = await request<BrainStatus>("system.status");
      setBrain(status);
      try {
        setOperations(negotiatedOperations(parseContractDescription(await request<unknown>("contract.describe"))));
      } catch {
        setOperations(new Set());
      }
    } catch (error) {
      setBrain(null);
      setConnectionError(errorMessage(error));
    } finally { setConnecting(false); }
  }, []);
  useEffect(() => { void connect(); }, [connect]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPage("Search");
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        setPage("Capture");
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, []);

  const unavailable = busy || connecting || !brain;
  const draftBytes = new TextEncoder().encode(draft).length;
  const draftTooLong = draftBytes > 16 * 1024;
  const clientName = setup.client === "claude-code" ? "Claude Code" : "Codex";

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || draftTooLong || unavailable) return;
    const attempt = pendingSave ?? { id: "plugin_" + crypto.randomUUID(), text: draft };
    setPendingSave(attempt);
    setSaveError("");
    setSavedText("");
    setBusy(true);
    try {
      await request("capture.create", { text: attempt.text }, attempt.id);
      setSavedText(attempt.text);
      setDraft("");
      setPendingSave(null);
      setBrain(previous => previous ? { ...previous, initialized: true } : previous);
      record("Captured to inbox", "Ready for search or explicit review before vault publication.");
    } catch (error) {
      setSaveError(saveMayHaveCompleted(error)
        ? "The save was not confirmed. Retry this same note to check the original save without creating another one."
        : errorMessage(error));
      if (!saveMayHaveCompleted(error)) setPendingSave(null);
      record("Save needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }

  function updateSetup(patch: Partial<SetupInput>) {
    setSetup(previous => ({ ...previous, ...patch }));
    setPreview(null);
    setSetupResult(null);
    setSetupError("");
  }
  function setupArguments(): SetupInput {
    return { ...setup, project_dir: setup.scope === "project" ? setup.project_dir?.trim() : null };
  }
  async function previewSetup(event: FormEvent) {
    event.preventDefault();
    if (!setupReady(setup) || unavailable) return;
    setBusy(true);
    setPreview(null);
    setSetupResult(null);
    setSetupError("");
    try { setPreview(await request<SetupPreview>("agent.setup.preview", setupArguments())); }
    catch (error) { setSetupError(errorMessage(error)); }
    finally { setBusy(false); }
  }
  async function applySetup() {
    if (!preview || unavailable) return;
    setBusy(true);
    setSetupError("");
    try {
      const result = await request<SetupResult>("agent.setup.apply", { ...setupArguments(), preview_id: preview.preview_id });
      setSetupResult(result);
      setPreview(null);
      record(clientName + " setup " + result.status, setup.scope === "project" ? "Project scope" : "User scope");
    } catch (error) {
      setSetupError(errorMessage(error));
      setPreview(null);
      record(clientName + " setup needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }

  async function refreshCollector() {
    if (unavailable) return;
    setBusy(true);
    setCollectorError("");
    try {
      const result = await request<CollectorStatus>("collector.status");
      setCollector(result);
      record("Collector status refreshed", result.sources.length + (result.sources.length === 1 ? " source" : " sources"));
    } catch (error) {
      setCollectorError(errorMessage(error));
      record("Collector status needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }
  function updateCollectorSetup(patch: Partial<CollectorEnableInput>) {
    setCollectorSetup(previous => ({ ...previous, ...patch }));
    setCollectorError("");
  }
  async function collectorCommand(operation: "collector.pause" | "collector.resume" | "collector.disable" | "collector.sync_now", sourceId: string) {
    if (unavailable) return;
    setBusy(true);
    setCollectorError("");
    try {
      await request(operation, { source_id: sourceId });
      const result = await request<CollectorStatus>("collector.status");
      setCollector(result);
      record(operation === "collector.sync_now" ? "Collector sync requested" : "Collector control saved", sourceId);
    } catch (error) {
      setCollectorError(errorMessage(error));
      record("Collector control needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }
  async function enableCollector(event: FormEvent) {
    event.preventDefault();
    if (unavailable) return;
    setBusy(true);
    setCollectorError("");
    try {
      const payload = {
        ...collectorSetup,
        source_id: collectorSetup.source_id.trim(),
        connector_name: collectorSetup.connector_name.trim(),
        connection_id: collectorSetup.connection_id.trim(),
        credential_ref: collectorSetup.credential_ref.trim(),
        resource_id: collectorSetup.resource_id.trim(),
        resource_type: collectorSetup.resource_type.trim(),
      };
      await request("collector.enable", payload);
      const result = await request<CollectorStatus>("collector.status");
      setCollector(result);
      record("Collector source enabled", payload.source_id);
    } catch (error) {
      setCollectorError(errorMessage(error));
      record("Collector setup needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }
  async function updateCollectorSchedule(sourceId: string, intervalSeconds: number) {
    if (unavailable) return;
    setBusy(true);
    setCollectorError("");
    try {
      await request("collector.schedule", { source_id: sourceId, interval_seconds: intervalSeconds });
      const result = await request<CollectorStatus>("collector.status");
      setCollector(result);
      record("Collector schedule saved", sourceId);
    } catch (error) {
      setCollectorError(errorMessage(error));
      record("Collector schedule needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark" aria-hidden="true">ob</span><span>Open Brain</span></div>
      <p className="sidebar-label">Your memory</p>
      <nav aria-label="Main navigation">{destinations.map((destination, index) =>
        <button key={destination} type="button" className={"nav-item " + (page === destination ? "selected" : "")}
          aria-current={page === destination ? "page" : undefined} onClick={() => setPage(destination)}>
          <span className="nav-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>{destination}
          {destination === "Search" ? <kbd>⌘ K</kbd> : null}
        </button>)}</nav>
      <div className="sidebar-bottom">
        <div className="runtime-state"><span className={"state-dot " + (brain ? "ready" : "")} />{connecting ? "Opening runtime…" : brain ? "Local runtime ready" : "Runtime unavailable"}</div>
        <p>Desktop, CLI, and agents.<br />One shared Brain.</p>
        <button type="button" className="text-button" onClick={() => setPage("Settings")}>Brain settings <span aria-hidden="true">↗</span></button>
      </div>
    </aside>
    <main className="workspace">
      <header className="page-header"><span>Personal workspace</span><span className="local-label">Stored locally</span></header>
      <div className="page-content">
        {connectionError ? <div className="notice error" role="alert"><p>{connectionError}</p><button type="button" onClick={() => void connect(true)} disabled={busy || connecting}>Retry connection</button></div> : null}
        {connecting ? <div className="notice" role="status">Opening the local runtime. First startup can take several seconds.</div> : null}

        {page === "Search" ? <SearchPanel disabled={unavailable} operations={operations} onCapture={() => setPage("Capture")} /> : null}

        {page === "Capture" ? <section aria-labelledby="capture-title">
          <div className="page-title"><p className="eyebrow">Capture</p><h1 id="capture-title">Keep the useful part.</h1><p>A thought, a decision, or the context you will need later.</p></div>
          <form onSubmit={event => void save(event)}>
            <label className="field-label" htmlFor="capture-text">What would you like to remember?</label>
            <textarea id="capture-text" className="capture-editor" rows={10} maxLength={16000} readOnly={Boolean(pendingSave)} value={draft}
              onChange={event => { setDraft(event.target.value); setSavedText(""); setSaveError(""); }} placeholder="Write or paste a note…" />
            <div className="form-footer"><span>{(draftBytes / 1024).toFixed(1)} / 16 KB</span><button type="submit" disabled={unavailable || !draft.trim() || draftTooLong}>{busy ? "Saving…" : pendingSave ? "Retry this save" : "Save to Brain"}</button></div>
          </form>
          {draftTooLong ? <p className="notice error" role="alert">That note is too long. Shorten it before saving.</p> : null}
          {saveError ? <p className="notice error" role="alert">{saveError}</p> : null}
          {savedText ? <div className="notice success" role="status"><p><strong>Captured to your inbox.</strong> Publication to the managed vault requires review and approval.</p><button type="button" className="secondary" disabled={unavailable} onClick={() => setPage("Review")}>Review captures for vault</button></div> : null}
          <p className="helper-text">Saving here does not send the note to a model. Agents can read it when you grant search access.</p>
        </section> : null}

        {page === "Review" ? <PublicationPanel disabled={unavailable} /> : null}

        {page === "Sources" ? <section aria-labelledby="sources-title">
          <div className="page-title"><p className="eyebrow">Sources</p><h1 id="sources-title">Bring your context together.</h1><p>Choose where memories come from and who can retrieve them.</p></div>
          <h2 className="section-heading">AI agents</h2>
          <div className="source-list">{(["claude-code", "codex"] as const).map(client => <div className="source-row" key={client}>
            <div><h3>{client === "claude-code" ? "Claude Code" : "Codex"}</h3><p>Explicit memory saves and search through MCP.</p></div>
            <button type="button" className="secondary" disabled={busy} onClick={() => { updateSetup({ client, action: "configure" }); setPage("Settings"); }}>Set up {client === "claude-code" ? "Claude Code" : "Codex"}</button>
          </div>)}</div>
          <SourceCapturePanel disabled={connecting || !brain} />
          <h2 className="section-heading upcoming-heading">Unattended collector</h2>
          <div className="collector-panel">
            <div><h3>Background imports</h3><p>Optional collection stays disabled until a source is explicitly enabled.</p></div>
            <button type="button" className="secondary" disabled={unavailable || busy} onClick={() => void refreshCollector()}>{busy ? "Checking..." : "Refresh status"}</button>
          </div>
          <form className="collector-enable" onSubmit={event => void enableCollector(event)}>
            <label>Source ID<input value={collectorSetup.source_id} onChange={event => updateCollectorSetup({ source_id: event.target.value })} spellCheck={false} /></label>
            <label>Connection<input value={collectorSetup.connection_id} onChange={event => updateCollectorSetup({ connection_id: event.target.value })} spellCheck={false} /></label>
            <label>Credential<input value={collectorSetup.credential_ref} onChange={event => updateCollectorSetup({ credential_ref: event.target.value })} spellCheck={false} /></label>
            <label>Resource<input value={collectorSetup.resource_id} onChange={event => updateCollectorSetup({ resource_id: event.target.value })} spellCheck={false} /></label>
            <label>Every<input type="number" min="1" max="31536000" step="1" value={collectorSetup.interval_seconds} onChange={event => updateCollectorSetup({ interval_seconds: Number(event.target.value) })} /></label>
            <button type="submit" disabled={unavailable || busy || !collectorSetup.source_id.trim() || !collectorSetup.connection_id.trim() || !collectorSetup.credential_ref.trim() || !collectorSetup.resource_id.trim()}>Enable source</button>
          </form>
          {collectorError ? <p className="notice error" role="alert">{collectorError}</p> : null}
          {collector ? <div className="source-list collector-list">{collector.sources.length ? collector.sources.map(source => <div className="source-row collector-row" key={source.source_id}>
            <div><h3>{source.source_id}</h3><p>{source.status} · next run {source.next_run_epoch ?? "not scheduled"} · captured {source.captured_count}</p>
              <label className="inline-schedule">Every <input type="number" min="1" max="31536000" step="1" defaultValue={source.interval_seconds} onBlur={event => void updateCollectorSchedule(source.source_id, Number(event.currentTarget.value))} /> seconds</label></div>
            <div className="row-actions">
              <button type="button" className="secondary" disabled={busy || source.status !== "enabled"} onClick={() => void collectorCommand("collector.pause", source.source_id)}>Pause</button>
              <button type="button" className="secondary" disabled={busy || source.status !== "paused"} onClick={() => void collectorCommand("collector.resume", source.source_id)}>Resume</button>
              <button type="button" className="secondary" disabled={busy || source.status === "disabled"} onClick={() => void collectorCommand("collector.disable", source.source_id)}>Disable</button>
              <button type="button" disabled={busy || source.status !== "enabled"} onClick={() => void collectorCommand("collector.sync_now", source.source_id)}>Sync now</button>
            </div>
          </div>) : <div className="empty-state compact-empty"><h2>No enabled collector sources.</h2><p>Use the collector CLI or provider setup flow to opt in a source first.</p></div>}</div> : null}
          <div className="subtle-note"><strong>You control capture and search separately.</strong><p>Explicit memory tools and automatic project capture have separate permissions.</p></div>
        </section> : null}

        {page === "Activity" ? <section aria-labelledby="activity-title">
          <div className="page-title"><p className="eyebrow">Activity</p><h1 id="activity-title">See what happened.</h1><p>Completed operations and errors from this app session.</p></div>
          {activity.length ? <ol className="activity-list">{activity.map(item => <li key={item.id}>
            <span className={"activity-marker " + (item.failed ? "failed" : "")} aria-hidden="true" /><div><h2>{item.title}</h2><p>{item.detail}</p></div><time dateTime={item.at.toISOString()}>{item.at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>
          </li>)}</ol> : <div className="empty-state"><h2>Nothing to report yet.</h2><p>Save a note, run a search, or configure an agent to see the result here.</p><button type="button" className="secondary" onClick={() => setPage("Capture")}>Capture a note</button></div>}
          <p className="helper-text">This view resets when the app closes. Your saved Brain content remains on disk.</p>
        </section> : null}

        {page === "Settings" ? <section aria-labelledby="settings-title">
          <div className="page-title"><p className="eyebrow">Settings</p><h1 id="settings-title">One Brain. Your tools.</h1><p>Use the desktop, a terminal, or your preferred coding agent.</p></div>
          <div className="brain-location"><div><h2>Brain location</h2><p className="selectable-path">{brain?.brain_root ?? "Waiting for the local runtime"}</p></div><button type="button" className="secondary" disabled={busy || connecting} onClick={() => void connect(true)}>Reconnect</button></div>
          <div className="brain-location collector-settings"><div><h2>Unattended collector</h2><p>{collector ? collector.sources.length + (collector.sources.length === 1 ? " configured source" : " configured sources") : "Refresh collector status from Sources."}</p></div><button type="button" className="secondary" disabled={unavailable || busy} onClick={() => { setPage("Sources"); void refreshCollector(); }}>Manage collector</button></div>
          <h2 className="section-heading">Agent setup</h2>
          <form onSubmit={event => void previewSetup(event)}><fieldset disabled={busy}>
            <div className="field-grid"><label>Agent<select value={setup.client} onChange={event => updateSetup({ client: event.target.value as SetupInput["client"] })}><option value="claude-code">Claude Code</option><option value="codex">Codex</option></select></label>
              <label>Scope<select value={setup.scope} onChange={event => updateSetup({ scope: event.target.value as SetupInput["scope"] })}><option value="project">This project</option><option value="user">All projects (user)</option></select></label></div>
            {setup.scope === "project" ? <label className="block-field">Project directory<input value={setup.project_dir ?? ""} onChange={event => updateSetup({ project_dir: event.target.value })} placeholder="/absolute/path/to/your/project" spellCheck={false} autoCapitalize="none" /></label> : null}
            <div className="permission-options">
              <label><input type="checkbox" checked={setup.allow_capture} onChange={event => updateSetup({ allow_capture: event.target.checked })} /><span><strong>Allow memory saves</strong><small>The agent can save facts when you ask. Full conversations are not collected.</small></span></label>
              <label><input type="checkbox" checked={setup.allow_search} onChange={event => updateSetup({ allow_search: event.target.checked })} /><span><strong>Allow Brain search</strong><small>The agent can read the whole Brain. Returned content may be sent to its model provider.</small></span></label>
            </div>
            <div className="setup-actions"><button type="submit" disabled={unavailable || !setupReady(setup)}>{busy ? "Working…" : setup.action === "remove" ? "Preview removal" : "Preview setup"}</button>
              <button type="button" className="text-button" onClick={() => updateSetup({ action: setup.action === "configure" ? "remove" : "configure" })}>{setup.action === "configure" ? "Remove an existing setup" : "Configure an agent instead"}</button></div>
          </fieldset></form>
          {setupError ? <p className="notice error" role="alert">{setupError}</p> : null}
          {preview ? <div className="setup-preview" aria-label="Configuration preview"><div className="section-label"><h2>{preview.action === "remove" ? "Review removal" : "Review setup"}</h2><span>Nothing changed yet</span></div>
            <p className="helper-text">Target Brain: <span className="selectable-path">{preview.brain_root}</span></p>
            {preview.changes.map((change, index) => <details key={change.path + index} open><summary><span>{change.operation}</span> {change.path}</summary><pre>{change.content || "No change needed."}</pre></details>)}
            {preview.notices?.map(notice => <p className="helper-text" key={notice}>{notice}</p>)}
            <button type="button" disabled={unavailable} onClick={() => void applySetup()}>{busy ? "Applying…" : preview.action === "remove" ? "Apply removal" : "Apply setup"}</button>
          </div> : null}
          {setupResult ? <div className="notice success" role="status"><p><strong>{clientName}: {setupResult.status}.</strong></p>
            {setup.action !== "remove" ? <><p>Start a fresh client session. Complete the client's normal project trust or MCP approval prompts.</p>
              {setup.allow_capture ? <p>Try: <code>Use Open Brain to save this fact: my memory test word is cedar.</code></p> : null}
              {setup.allow_search ? <p>In a new session: <code>Search Open Brain for my memory test word.</code></p> : <p>Search is disabled for this setup.</p>}
            </> : <p>{setupResult.status === "unchanged" ? "No owned setup needed removal." : "The owned setup was removed."} Existing Brain content is preserved.</p>}
            {setupResult.notices?.map(notice => <p key={notice}>{notice}</p>)}</div> : null}
          <div className="subtle-note"><h2>Prefer a terminal?</h2><p>The same setup is available with <code>open-brain agent setup --help</code>. The desktop can stay closed while an agent uses its own foreground MCP connection.</p></div>
        </section> : null}
      </div>
    </main>
  </div>;
}
