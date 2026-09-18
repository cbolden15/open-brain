import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  errorMessage,
  readCompleteRecord,
  request,
  type RecordSummary,
  type SearchFilters,
  type SearchMode,
  type SearchPage,
} from "./client";
import { parseSearchPage, validateT03Wire } from "./t03-wire";

type SearchSnapshot = { query: string; filters: SearchFilters; mode: SearchMode };

export function SearchPanel({ disabled, operations, onCapture }: {
  disabled: boolean;
  operations: ReadonlySet<string>;
  onCapture: () => void;
}) {
  const [query, setQuery] = useState("");
  const [spaceIds, setSpaceIds] = useState("");
  const [payloadFamilies, setPayloadFamilies] = useState<SearchFilters["payload_families"]>([]);
  const [recordTypes, setRecordTypes] = useState<SearchFilters["record_types"]>([]);
  const [mode, setMode] = useState<SearchMode>("lexical");
  const [snapshot, setSnapshot] = useState<SearchSnapshot | null>(null);
  const [hits, setHits] = useState<RecordSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);
  const [modeUsed, setModeUsed] = useState<"lexical" | "hybrid" | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [opened, setOpened] = useState<{ record: RecordSummary; text: string } | null>(null);
  const readAbort = useRef<AbortController | null>(null);
  const available = operations.has("search.page");
  const canRead = operations.has("record.read");

  useEffect(() => {
    const cancel = (event: KeyboardEvent) => {
      if (event.key === "Escape" && readAbort.current) {
        event.preventDefault();
        readAbort.current.abort();
        readAbort.current = null;
        setBusy(false);
        setOpened(null);
      }
    };
    window.addEventListener("keydown", cancel);
    return () => {
      window.removeEventListener("keydown", cancel);
      readAbort.current?.abort();
    };
  }, []);

  function toggle<T extends string>(values: T[], value: T, checked: boolean): T[] {
    return checked ? [...values, value] : values.filter(item => item !== value);
  }

  function currentSnapshot(): SearchSnapshot {
    return {
      query: query.trim(),
      filters: {
        space_ids: spaceIds.split(",").map(value => value.trim()).filter(Boolean),
        payload_families: payloadFamilies,
        record_types: recordTypes,
      },
      mode,
    };
  }

  async function runSearch(event?: FormEvent, continuation = false, restart = false) {
    event?.preventDefault();
    const selected = continuation || restart ? snapshot : currentSnapshot();
    if (!selected?.query || disabled || busy || !available) return;
    setBusy(true);
    setError("");
    setStale(false);
    if (!continuation) setOpened(null);
    try {
      const wireRequest = {
        dto_version: 1,
        query: selected.query,
        filters: selected.filters,
        mode: selected.mode,
        limit: 100,
        cursor: continuation ? nextCursor : null,
      };
      validateT03Wire("search.page.request", wireRequest);
      const page: SearchPage = parseSearchPage(await request<unknown>("search.page", wireRequest));
      setSnapshot(selected);
      setHits(previous => {
        const combined = continuation ? [...previous, ...page.results] : page.results;
        const unique = new Map(combined.map(hit => [`${hit.record_id}\0${hit.revision_id}`, hit]));
        return [...unique.values()];
      });
      setNextCursor(page.next_cursor);
      setComplete(page.complete);
      setModeUsed(page.mode_used);
      setWarnings(page.warnings);
    } catch (caught) {
      if (String(caught) === "cursor_stale") setStale(true);
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  async function openRecord(hit: RecordSummary) {
    if (!canRead || busy) return;
    const controller = new AbortController();
    readAbort.current?.abort();
    readAbort.current = controller;
    setBusy(true);
    setError("");
    setOpened(null);
    try {
      const record = await readCompleteRecord(hit, controller.signal);
      if (!controller.signal.aborted) setOpened(record);
    } catch (caught) {
      if (String(caught) !== "cancelled") setError(errorMessage(caught));
    } finally {
      if (readAbort.current === controller) readAbort.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <section aria-labelledby="search-title">
    <div className="page-title"><p className="eyebrow">Search</p><h1 id="search-title">Find what you saved.</h1><p>Read complete captures and notes from your shared Brain.</p></div>
    {!available ? <p className="notice" role="status">Paged search is unavailable in this runtime. Update Open Brain to search and read complete records here.</p> : null}
    <form className="search-form search-controls" onSubmit={event => void runSearch(event)}>
      <label className="visually-hidden" htmlFor="search-input">Search your Brain</label>
      <input id="search-input" type="search" placeholder="A person, project, or something you remember…" value={query} maxLength={500} onChange={event => setQuery(event.target.value)} />
      <button type="submit" disabled={disabled || busy || !available || !query.trim()}>{busy ? "Searching…" : "Search"}</button>
      <details className="search-filters"><summary>Filters and mode</summary>
        <label>Space IDs<input value={spaceIds} onChange={event => setSpaceIds(event.target.value)} placeholder="space_… separated by commas" /></label>
        <fieldset><legend>Record type</legend>{(["source", "canonical"] as const).map(value => <label key={value}><input type="checkbox" checked={recordTypes.includes(value)} onChange={event => setRecordTypes(toggle(recordTypes, value, event.target.checked))} />{value === "source" ? "Captures" : "Published notes"}</label>)}</fieldset>
        <fieldset><legend>Payload</legend>{(["text", "event", "measurement", "reference_or_file"] as const).map(value => <label key={value}><input type="checkbox" checked={payloadFamilies.includes(value)} onChange={event => setPayloadFamilies(toggle(payloadFamilies, value, event.target.checked))} />{value.replaceAll("_", " ")}</label>)}</fieldset>
        <label>Search mode<select value={mode} onChange={event => setMode(event.target.value as SearchMode)}><option value="lexical">Lexical</option><option value="hybrid_preferred">Hybrid when available</option><option value="hybrid_required">Require hybrid</option></select></label>
      </details>
    </form>
    {error ? <p className="notice error" role="alert">{error}</p> : null}
    {stale ? <button type="button" className="secondary" onClick={() => void runSearch(undefined, false, true)}>Restart this search</button> : null}
    {snapshot ? <div className="results" aria-live="polite">
      <div className="section-label"><span>{hits.length} {hits.length === 1 ? "match" : "matches"}</span><span>{modeUsed ?? "searching"}{warnings.length ? ` · ${warnings.join(", ")}` : ""}</span></div>
      {hits.map(hit => <article className="search-result" key={`${hit.record_id}:${hit.revision_id}`}>
        <div className="result-meta"><span>{hit.record_type === "source" ? "Capture" : "Published note"}</span><span>{hit.provenance.source_origin === "owner_authored" ? "Owner authored" : "Unverified source"}</span><span>Revision {hit.revision_id}</span></div>
        <h2>{hit.title}</h2><p>{hit.excerpt}</p>
        <button type="button" className="secondary" disabled={!canRead || busy} onClick={() => void openRecord(hit)}>{canRead ? "Read complete record" : "Complete read unavailable"}</button>
      </article>)}
      {!hits.length ? <div className="empty-state"><h2>No matches yet.</h2><p>Try fewer words, change filters, or capture the detail you want to remember.</p><button type="button" className="secondary" onClick={onCapture}>Capture a note</button></div> : null}
      {!complete && nextCursor && !stale ? <button type="button" disabled={busy} onClick={() => void runSearch(undefined, true)}>Load more results</button> : null}
    </div> : null}
    {opened ? <article className="record-reader" aria-label="Complete record"><div className="result-meta"><span>{opened.record.record_type === "source" ? "Capture" : "Published note"}</span><span>{opened.record.revision_id}</span></div><h2>{opened.record.title}</h2><pre>{opened.text}</pre><button type="button" className="secondary" onClick={() => setOpened(null)}>Close record</button></article> : null}
  </section>;
}
