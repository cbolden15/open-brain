import { invoke } from "@tauri-apps/api/core";
import { useEffect, useRef, useState } from "react";
import { errorMessage, request, type InboxItem, type PublicationInspection, type Space, type WorkspaceRefresh } from "./client";

type Proposal = { proposal_id: string; page_id: string };
type Decision = { status: string; outcome: string; proposal_id: string; page_id: string; publication_id?: string };

export function PublicationPanel({ disabled }: { disabled: boolean }) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [spaceId, setSpaceId] = useState("");
  const [newSpace, setNewSpace] = useState("");
  const [title, setTitle] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [inspection, setInspection] = useState<PublicationInspection | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [approvedNote, setApprovedNote] = useState<{ vaultPath: string; relativePath: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const flowVersion = useRef(0);
  const decisionPending = useRef(false);

  useEffect(() => {
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || decisionPending.current || (!proposal && !markdown)) return;
      event.preventDefault();
      flowVersion.current += 1;
      setProposal(null);
      setInspection(null);
      setDecision(null);
      setApprovedNote(null);
      setTitle("");
      setMarkdown("");
      setBusy(false);
      setError("Draft review cancelled. No publication decision was sent.");
    };
    window.addEventListener("keydown", cancel);
    return () => {
      flowVersion.current += 1;
      window.removeEventListener("keydown", cancel);
    };
  }, [proposal, markdown]);

  async function ensureWorkspace() {
    try {
      await request("workspace.status");
    } catch (caught) {
      if (String(caught) !== "setup_required") throw caught;
      await request("workspace.setup");
      await request("workspace.status");
    }
  }

  async function load(offset = 0) {
    if (disabled || busy) return;
    setBusy(true);
    setError("");
    try {
      await ensureWorkspace();
      const inbox = await request<{ items: InboxItem[]; next_offset: number | null }>("inbox.list", { dto_version: 1, unassigned_only: false, limit: 50, offset });
      const listed = await request<{ spaces: Space[] }>("space.list", { dto_version: 1, limit: 50, offset: 0 });
      setItems(previous => offset ? [...previous, ...inbox.items] : inbox.items);
      setNextOffset(inbox.next_offset);
      setSpaces(listed.spaces);
      setLoaded(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function toggle(item: InboxItem, checked: boolean) {
    setError("");
    if (!checked) {
      setSelected(previous => previous.filter(id => id !== item.capture_id));
      return;
    }
    if (selected.length >= 32) {
      setError("Choose at most 32 captures for one publication.");
      return;
    }
    const selectedSpaces = new Set(items.filter(row => selected.includes(row.capture_id)).map(row => row.space_id).filter(Boolean));
    if (item.space_id && selectedSpaces.size && !selectedSpaces.has(item.space_id)) {
      setError("Choose captures from one space. Route them to the same space first.");
      return;
    }
    setSelected(previous => [...previous, item.capture_id]);
    if (item.space_id) setSpaceId(item.space_id);
  }

  async function routeSelected() {
    if (!selected.length || busy) return;
    setBusy(true);
    setError("");
    try {
      let destination = spaceId;
      if (!destination) {
        if (!newSpace.trim()) throw "invalid_arguments";
        const created = await request<{ space: Space }>("space.create", { dto_version: 1, name: newSpace.trim() });
        destination = created.space.space_id;
        setSpaces(previous => [...previous, created.space]);
        setSpaceId(destination);
      }
      for (const captureId of selected) {
        const row = items.find(item => item.capture_id === captureId);
        if (row?.space_id !== destination) {
          await request("inbox.route", { dto_version: 1, capture_id: captureId, space_id: destination });
        }
      }
      setItems(previous => previous.map(item => selected.includes(item.capture_id) ? { ...item, space_id: destination } : item));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function prepareDraft() {
    const chosen = selected.map(id => items.find(item => item.capture_id === id)).filter((item): item is InboxItem => Boolean(item));
    const selectedSpaces = new Set(chosen.map(item => item.space_id));
    if (!chosen.length || selectedSpaces.size !== 1 || chosen[0]?.space_id === null) {
      setError("Route 1–32 selected captures to one space before drafting.");
      return;
    }
    const first = chosen[0];
    if (!first) return;
    const draftTitle = first.title?.trim() || "New note";
    const body = chosen.map(item => item.preview.trim()).filter(Boolean).join("\n\n---\n\n");
    setTitle(draftTitle);
    setMarkdown(`# ${draftTitle}\n\n${body}\n`);
    setProposal(null);
    setInspection(null);
    setDecision(null);
    setApprovedNote(null);
    setError("");
  }

  async function inspect() {
    if (!title.trim() || !markdown.trim() || busy) return;
    const version = flowVersion.current;
    setBusy(true);
    setError("");
    try {
      const nextProposal = proposal ?? await request<Proposal>("publication.propose", {
        dto_version: 1,
        capture_ids: selected,
        title: title.trim(),
        markdown,
      });
      if (version !== flowVersion.current) return;
      setProposal(nextProposal);
      const shown = await request<PublicationInspection>("publication.show", { dto_version: 1, proposal_id: nextProposal.proposal_id });
      if (version === flowVersion.current) setInspection(shown);
    } catch (caught) {
      if (version === flowVersion.current) setError(errorMessage(caught));
    } finally {
      if (version === flowVersion.current) setBusy(false);
    }
  }

  async function decide(operation: "publication.approve" | "publication.edit_and_approve" | "publication.reject") {
    if (!proposal || !inspection || busy) return;
    decisionPending.current = true;
    setBusy(true);
    setError("");
    try {
      const arguments_: Record<string, unknown> = {
        dto_version: 1,
        proposal_id: proposal.proposal_id,
        review_token: inspection.review_token,
      };
      if (operation === "publication.edit_and_approve") arguments_.markdown = markdown;
      const result = await request<Decision>(operation, arguments_);
      setDecision(result);
      if (operation !== "publication.reject") {
        const refreshed = await request<WorkspaceRefresh>("workspace.refresh");
        const note = refreshed.notes.find(row => row.note_id === result.page_id);
        if (!note) throw "malformed_response";
        setApprovedNote({ vaultPath: refreshed.vault_path, relativePath: note.relative_path });
      }
    } catch (caught) {
      if (["review_conflict", "terminal_decision"].includes(String(caught))) setInspection(null);
      setError(errorMessage(caught));
    } finally {
      decisionPending.current = false;
      setBusy(false);
    }
  }

  async function openApprovedNote() {
    if (!approvedNote || busy) return;
    setBusy(true);
    setError("");
    try {
      await invoke("desktop_reveal_managed_note", { vaultPath: approvedNote.vaultPath, relativePath: approvedNote.relativePath });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  const chosen = items.filter(item => selected.includes(item.capture_id));
  const routedTogether = chosen.length > 0 && new Set(chosen.map(item => item.space_id)).size === 1 && chosen[0]?.space_id !== null;
  const editedAfterInspection = inspection ? title.trim() !== inspection.title || markdown !== inspection.markdown : false;

  return <section aria-labelledby="review-title">
    <div className="page-title"><p className="eyebrow">Review</p><h1 id="review-title">Publish captures to your vault.</h1><p>Choose evidence, edit the complete draft, inspect it, then make an explicit decision.</p></div>
    {!loaded ? <button type="button" disabled={disabled || busy} onClick={() => void load()}>{busy ? "Preparing vault…" : "Review inbox captures"}</button> : null}
    {error ? <p className="notice error" role="alert">{error}</p> : null}
    {loaded && !markdown ? <>
      <div className="section-label"><span>{selected.length} selected</span><span>1–32 captures · one space</span></div>
      <div className="capture-selection">{items.map(item => <label className="capture-choice" key={item.capture_id}><input type="checkbox" checked={selected.includes(item.capture_id)} onChange={event => toggle(item, event.target.checked)} /><span><strong>{item.title || "Untitled capture"}</strong><small>{item.space_id ? spaces.find(space => space.space_id === item.space_id)?.name || item.space_id : "Inbox · needs a space"}</small><span>{item.preview}</span></span></label>)}</div>
      {nextOffset !== null ? <button type="button" className="secondary" disabled={busy} onClick={() => void load(nextOffset)}>Load more captures</button> : null}
      {selected.length ? <div className="route-controls"><label>Route selected to<select value={spaceId} onChange={event => setSpaceId(event.target.value)}><option value="">Create a new space</option>{spaces.map(space => <option value={space.space_id} key={space.space_id}>{space.name}</option>)}</select></label>{!spaceId ? <label>New space name<input value={newSpace} maxLength={120} onChange={event => setNewSpace(event.target.value)} /></label> : null}<button type="button" className="secondary" disabled={busy || (!spaceId && !newSpace.trim())} onClick={() => void routeSelected()}>Route selected</button><button type="button" disabled={busy || !routedTogether} onClick={prepareDraft}>Create editable draft</button></div> : null}
    </> : null}
    {markdown && !decision ? <div className="publication-draft">
      <label>Title<input value={title} maxLength={500} readOnly={Boolean(proposal)} onChange={event => setTitle(event.target.value)} /></label>
      <label>Complete Markdown draft<textarea rows={14} value={markdown} onChange={event => setMarkdown(event.target.value)} /></label>
      {!inspection ? <button type="button" disabled={busy || !title.trim() || !markdown.trim()} onClick={() => void inspect()}>{proposal ? "Inspect proposal again" : "Inspect proposal and evidence"}</button> : <div className="inspection" aria-label="Publication inspection"><h2>Inspect exact proposal</h2><p>{inspection.operation} · {inspection.selected_capture_ids.length} evidence captures</p><pre>{inspection.markdown}</pre><ul>{inspection.evidence.map(row => <li key={row.capture_id}>{row.capture_id}: {row.excerpt}</li>)}</ul><div className="row-actions"><button type="button" disabled={busy || editedAfterInspection} onClick={() => void decide("publication.approve")}>Approve exact inspected draft</button><button type="button" disabled={busy || !editedAfterInspection} onClick={() => void decide("publication.edit_and_approve")}>Edit and approve</button><button type="button" className="secondary" disabled={busy} onClick={() => void decide("publication.reject")}>Reject proposal</button></div></div>}
      <p className="helper-text">Press Escape to cancel this review. Cancelling does not send an approval or rejection.</p>
    </div> : null}
    {decision ? <div className="notice success" role="status"><p><strong>{decision.outcome === "rejected" ? "Proposal rejected." : "Published to the managed vault."}</strong></p>{approvedNote ? <button type="button" disabled={busy} onClick={() => void openApprovedNote()}>Open managed note</button> : null}</div> : null}
  </section>;
}
