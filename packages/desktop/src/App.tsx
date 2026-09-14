import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { errorMessage, request, saveMayHaveCompleted, setupReady, type BrainStatus, type SearchHit, type SetupInput, type SetupPreview, type SetupResult } from "./client";

const destinations = ["Search", "Capture", "Sources", "Activity", "Settings"] as const;
type Destination = typeof destinations[number];
type Activity = { id: string; title: string; detail: string; at: Date; failed: boolean };

export function App() {
  const [page, setPage] = useState<Destination>("Search");
  const [brain, setBrain] = useState<BrainStatus | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState<string | null>(null);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searchError, setSearchError] = useState("");
  const [draft, setDraft] = useState("");
  const [pendingSave, setPendingSave] = useState<{ id: string; text: string } | null>(null);
  const [saveError, setSaveError] = useState("");
  const [savedText, setSavedText] = useState("");
  const [activity, setActivity] = useState<Activity[]>([]);
  const [setup, setSetup] = useState<SetupInput>({
    client: "claude-code", scope: "project", project_dir: "",
    allow_capture: false, allow_search: false, action: "configure",
  });
  const [preview, setPreview] = useState<SetupPreview | null>(null);
  const [setupResult, setSetupResult] = useState<SetupResult | null>(null);
  const [setupError, setSetupError] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);

  const record = useCallback((title: string, detail: string, failed = false) => {
    setActivity(previous => [{ id: crypto.randomUUID(), title, detail, failed, at: new Date() }, ...previous].slice(0, 100));
  }, []);
  const connect = useCallback(async (restart = false) => {
    setConnecting(true);
    setConnectionError("");
    try {
      if (restart) await invoke("desktop_reconnect");
      setBrain(await request<BrainStatus>("system.status"));
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
        setTimeout(() => searchInput.current?.focus(), 0);
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

  async function search(event?: FormEvent, phrase = query) {
    event?.preventDefault();
    const term = phrase.trim();
    if (!term || unavailable) return;
    setBusy(true);
    setSearchError("");
    setPage("Search");
    setQuery(term);
    try {
      const result = await request<{ results: SearchHit[] }>("search.query", { query: term, limit: 30 });
      setHits(result.results);
      setSearched(term);
      record("Search completed", String(result.results.length) + (result.results.length === 1 ? " match" : " matches"));
    } catch (error) {
      setSearchError(errorMessage(error));
      record("Search needs attention", errorMessage(error), true);
    } finally { setBusy(false); }
  }

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
      record("Note saved", "Available to this desktop and permitted agent searches.");
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

        {page === "Search" ? <section aria-labelledby="search-title">
          <div className="page-title"><p className="eyebrow">Search</p><h1 id="search-title">Find what you saved.</h1><p>Bring a fact, decision, or idea back into focus.</p></div>
          <form className="search-form" onSubmit={event => void search(event)}>
            <label className="visually-hidden" htmlFor="search-input">Search your Brain</label>
            <input ref={searchInput} id="search-input" type="search" placeholder="A person, project, or something you remember…" value={query} maxLength={2000} onChange={event => setQuery(event.target.value)} />
            <button type="submit" disabled={unavailable || !query.trim()}>{busy ? "Searching…" : "Search"}</button>
          </form>
          {searchError ? <p className="notice error" role="alert">{searchError}</p> : null}
          {searched !== null ? <div className="results" aria-live="polite">
            <div className="section-label"><span>{hits.length} {hits.length === 1 ? "match" : "matches"}</span><span>For “{searched}”</span></div>
            {hits.length ? hits.map(hit => <article className="search-result" key={hit.result_id}>
              <div className="result-meta"><span>{hit.record_type === "source" ? "Capture" : "Note"}</span><span>{hit.source_origin === "owner_authored" ? "Owner authored" : "Unverified source"}</span></div>
              <h2>{hit.title}</h2><p>{hit.excerpt}</p>
            </article>) : <div className="empty-state"><h2>No matches yet.</h2><p>Try fewer words, or capture the detail you want to remember.</p><button type="button" className="secondary" onClick={() => setPage("Capture")}>Capture a note</button></div>}
          </div> : <div className="empty-state search-empty"><span className="empty-symbol" aria-hidden="true">↗</span><h2>{brain?.initialized ? "Your memory is a search away." : "Start with one thing worth remembering."}</h2><p>Save a note here or through a connected agent. It will be searchable in the same Brain.</p><button type="button" className="secondary" onClick={() => setPage("Capture")}>{brain?.initialized ? "Capture a note" : "Capture your first note"}</button></div>}
        </section> : null}

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
          {savedText ? <div className="notice success" role="status"><p><strong>Saved to your Brain.</strong> It is ready for search.</p><button type="button" className="secondary" disabled={unavailable} onClick={() => void search(undefined, savedText.trim().split(/\s+/).slice(0, 12).join(" "))}>Find this note</button></div> : null}
          <p className="helper-text">Saving here does not send the note to a model. Agents can read it when you grant search access.</p>
        </section> : null}

        {page === "Sources" ? <section aria-labelledby="sources-title">
          <div className="page-title"><p className="eyebrow">Sources</p><h1 id="sources-title">Bring your context together.</h1><p>Choose where memories come from and who can retrieve them.</p></div>
          <h2 className="section-heading">AI agents</h2>
          <div className="source-list">{(["claude-code", "codex"] as const).map(client => <div className="source-row" key={client}>
            <div><h3>{client === "claude-code" ? "Claude Code" : "Codex"}</h3><p>Explicit memory saves and search through MCP.</p></div>
            <button type="button" className="secondary" disabled={busy} onClick={() => { updateSetup({ client, action: "configure" }); setPage("Settings"); }}>Set up {client === "claude-code" ? "Claude Code" : "Codex"}</button>
          </div>)}</div>
          <h2 className="section-heading upcoming-heading">Account imports</h2>
          <p className="helper-text">Account imports are not available in this build. GitHub issues and pull requests are the next planned source.</p>
          <div className="planned-sources">{["GitHub", "Jira", "Email", "Google Drive", "iMessage", "Slack", "GitLab"].map(source => <span key={source}>{source}<small>Planned</small></span>)}</div>
          <div className="subtle-note"><strong>You control capture and search separately.</strong><p>Connecting an agent does not import its conversation history. Background collection is not enabled.</p></div>
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
