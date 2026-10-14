/** Closed view of v5/v6 publication evidence. Never grants authority. */
import { digest, exact, identifier, requireFacet, safeInteger, timestamp, validateFacetDefinition } from "./facet-wire";
import type { Wire_selector } from "./facet-types";

export interface FacetPublicationBinding {
  schema_version: 5 | 6; selection_kind: "facet";
  base_schema_version?: 5;
  development_evidence?: { capture_id: string; evidence_sha256: string | null }[];
  actor_id: string; tenant_id: string; recorded_at: string; review_digest: string;
  role_claim: { actor_id: string; tenant_id: string; role_id: string; role_claim_id: string; capabilities: string[] };
  operation: "create" | "update"; page_id: string; proposal_id: string;
  expected_page_sha256: string | null; expected_publication_id: string | null;
  destination_space_id: string; query_id: string; query_version: number; query_selection_version: number;
  attachment_version: number; selector: Wire_selector;
  selected_capture_ids: string[]; provenance: string[];
  source_states: { capture_id: string; route_id: string | null; sha256: string; space_id: string | null }[];
  facet_source_states: { source_id: string; capture_id: string; head_version: number; memberships: { value_id: string; membership_version: number }[] }[];
}
export interface FacetPublicationInspection {
  status: "shown"; proposal_id: string; proposal_status: "pending" | "approved" | "rejected" | "edited";
  title: string; markdown: string; space_id: string; page_id: string; target_page_id: string | null;
  operation: "create" | "update"; capture_ids: string[]; selected_capture_ids: string[];
  evidence: { capture_id: string; excerpt: string; sha256: string; projection_applied: boolean }[];
  review_token: string; expected_page_sha256: string | null; expected_publication_id: string | null;
  projection_applied: boolean; facet_selection: FacetPublicationBinding;
}
const equal = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);
function text(value: unknown, maximum: number): void {
  requireFacet(typeof value === "string" && !/[\u0000\ud800-\udfff]/u.test(value) && new TextEncoder().encode(value).length <= maximum);
}
function ids(value: unknown): string[] {
  requireFacet(Array.isArray(value) && value.length >= 1 && value.length <= 32 && value.every((id) => identifier(id, ["capture"], true)) && new Set(value).size === value.length);
  return [...value];
}
export function parseFacetPublicationBinding(value: unknown): FacetPublicationBinding {
  const development = typeof value === "object" && value !== null && "schema_version" in value && value.schema_version === 6;
  const b = exact(value, ["schema_version", "selection_kind", "actor_id", "tenant_id", "recorded_at", "review_digest", "role_claim", "operation", "page_id", "proposal_id", "expected_page_sha256", "expected_publication_id", "destination_space_id", "query_id", "query_version", "query_selection_version", "attachment_version", "selector", "selected_capture_ids", "provenance", "source_states", "facet_source_states", ...(development ? ["base_schema_version", "development_evidence"] : [])]);
  requireFacet((b.schema_version === 5 || development && b.base_schema_version === 5) && b.selection_kind === "facet");
  for (const [key, prefix] of Object.entries({ actor_id: "actor", tenant_id: "tenant", page_id: "page", proposal_id: "proposal", destination_space_id: "space", query_id: "facetquery" })) requireFacet(identifier(b[key], [prefix], true));
  requireFacet(typeof b.recorded_at === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(b.recorded_at));
  const canonicalTime = b.recorded_at.replace(/(?:\.(\d+))?Z$/, (_match: string, fraction: string | undefined) => "." + (fraction ?? "").padEnd(6, "0").slice(0, 6) + "Z");
  requireFacet(timestamp(canonicalTime) && digest(b.review_digest));
  const role = exact(b.role_claim, ["actor_id", "tenant_id", "role_id", "role_claim_id", "capabilities"]);
  requireFacet(role.actor_id === b.actor_id && role.tenant_id === b.tenant_id && identifier(role.role_id, ["role"], true) && identifier(role.role_claim_id, ["role_claim"], true));
  requireFacet(Array.isArray(role.capabilities) && role.capabilities.every((s: unknown) => typeof s === "string" && /^[a-z][a-z0-9_.-]{0,63}$/.test(s)) && equal(role.capabilities, [...new Set(role.capabilities)].sort()));
  requireFacet(["create", "update"].includes(b.operation));
  const create = b.operation === "create";
  requireFacet(create ? b.expected_page_sha256 === null && b.expected_publication_id === null : digest(b.expected_page_sha256) && identifier(b.expected_publication_id, ["publication"], true));
  requireFacet(safeInteger(b.query_version, 1) && safeInteger(b.query_selection_version, 1) && b.query_selection_version <= b.query_version && safeInteger(b.attachment_version) && (b.attachment_version === 0) === create);
  const selector = validateFacetDefinition("selector", b.selector) as Wire_selector;
  requireFacet(selector.record_types.length === 0 || selector.record_types.includes("source"));
  const selected = ids(b.selected_capture_ids), provenance = ids(b.provenance);
  requireFacet(selected.every((id) => provenance.includes(id)) && (!create || equal(selected, provenance)));
  if (development) {
    requireFacet(Array.isArray(b.development_evidence) && b.development_evidence.length >= 1 && b.development_evidence.length <= 1024);
    let previous = "";
    const committed = new Set<string>();
    for (const raw of b.development_evidence) {
      const item = exact(raw, ["capture_id", "evidence_sha256"]);
      requireFacet(identifier(item.capture_id, ["capture"], true) && item.capture_id > previous && (item.evidence_sha256 === null || digest(item.evidence_sha256)));
      previous = item.capture_id;
      committed.add(item.capture_id);
    }
    requireFacet(provenance.every((captureId) => committed.has(captureId)));
  }
  requireFacet(Array.isArray(b.source_states) && b.source_states.length === provenance.length);
  for (const [index, raw] of b.source_states.entries()) {
    const state = exact(raw, ["capture_id", "route_id", "sha256", "space_id"]);
    requireFacet(state.capture_id === provenance[index] && digest(state.sha256) && (state.route_id === null || identifier(state.route_id, ["route"], true)) && (state.space_id === null || identifier(state.space_id, ["space"], true)));
  }
  requireFacet(Array.isArray(b.facet_source_states) && b.facet_source_states.length === selected.length);
  const sources = new Set<string>();
  for (const [index, raw] of b.facet_source_states.entries()) {
    const state = exact(raw, ["source_id", "capture_id", "head_version", "memberships"]);
    requireFacet(identifier(state.source_id, ["source"], true) && !sources.has(state.source_id) && state.capture_id === selected[index] && safeInteger(state.head_version, 1)); sources.add(state.source_id);
    requireFacet(Array.isArray(state.memberships) && state.memberships.length === selector.all_value_ids.length);
    for (const [memberIndex, rawMember] of state.memberships.entries()) {
      const member = exact(rawMember, ["value_id", "membership_version"]);
      requireFacet(member.value_id === selector.all_value_ids[memberIndex] && safeInteger(member.membership_version, 1));
    }
  }
  return JSON.parse(JSON.stringify(b)) as FacetPublicationBinding;
}
export function parseFacetPublicationInspection(value: unknown): FacetPublicationInspection {
  const v = exact(value, ["status", "proposal_id", "proposal_status", "title", "markdown", "space_id", "page_id", "target_page_id", "operation", "capture_ids", "selected_capture_ids", "evidence", "review_token", "expected_page_sha256", "expected_publication_id", "projection_applied", "facet_selection"]);
  const binding = parseFacetPublicationBinding(v.facet_selection);
  requireFacet(v.status === "shown" && ["pending", "approved", "rejected", "edited"].includes(v.proposal_status) && typeof v.projection_applied === "boolean");
  text(v.title, 1048576); text(v.markdown, 1048576);
  requireFacet(v.proposal_id === binding.proposal_id && v.operation === binding.operation && v.page_id === binding.page_id && v.space_id === binding.destination_space_id && v.review_token === binding.review_digest);
  requireFacet(v.target_page_id === (binding.operation === "create" ? null : binding.page_id) && v.expected_page_sha256 === binding.expected_page_sha256 && v.expected_publication_id === binding.expected_publication_id);
  requireFacet(equal(ids(v.capture_ids), binding.provenance) && equal(ids(v.selected_capture_ids), binding.selected_capture_ids));
  requireFacet(Array.isArray(v.evidence) && v.evidence.length === binding.provenance.length);
  for (const [index, raw] of v.evidence.entries()) {
    const evidence = exact(raw, ["capture_id", "excerpt", "sha256", "projection_applied"]);
    // ReviewEvidence hashes its displayed excerpt, not the immutable source payload.
    requireFacet(evidence.capture_id === binding.provenance[index] && digest(evidence.sha256) && typeof evidence.projection_applied === "boolean");
    text(evidence.excerpt, 1048576);
  }
  return { ...JSON.parse(JSON.stringify(v)), facet_selection: binding } as FacetPublicationInspection;
}
