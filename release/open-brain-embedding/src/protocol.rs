use crate::component::{Component, sha256_hex};
use crate::inference::Inference;
use crate::json::{self, Value, quote};
use crate::tokenize::{self, TokenError};
use std::path::Path;
use unicode_normalization::is_nfc;

pub const PROTOCOL: &str = "open-brain.embedding-helper.v1";
pub const ZERO_ID: &str = "00000000000000000000000000000000";

pub const MAX_REQUEST_BYTES: usize = 131_072;
pub const MAX_RESPONSE_BYTES: usize = 1_048_576;
pub const MAX_STDERR_BYTES: usize = 4096;
pub const MAX_ITEMS: usize = 4;
pub const MAX_TOTAL_CHUNKS: usize = 128;
pub const MAX_DOCUMENT_BYTES: usize = 65_536;
pub const MAX_QUERY_BYTES: usize = 8192;
pub const DIMENSIONS: usize = 384;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Code {
    ProtocolUnsupported,
    RequestInvalid,
    ComponentMismatch,
    InputLimit,
    TokenizationFailed,
    InferenceFailed,
}

impl Code {
    pub fn as_str(self) -> &'static str {
        match self {
            Code::ProtocolUnsupported => "protocol_unsupported",
            Code::RequestInvalid => "request_invalid",
            Code::ComponentMismatch => "component_mismatch",
            Code::InputLimit => "input_limit",
            Code::TokenizationFailed => "tokenization_failed",
            Code::InferenceFailed => "inference_failed",
        }
    }
}

/// A complete framed response (JSON + LF) and the process exit code.
#[derive(Debug)]
pub struct Outcome {
    pub body: Vec<u8>,
    pub exit: i32,
    /// Text-free category for stderr, present on framed errors only.
    pub category: Option<&'static str>,
}

fn error(id: &str, code: Code) -> Outcome {
    let body = format!(
        "{{\"protocol\":\"{PROTOCOL}\",\"request_id\":\"{id}\",\"status\":\"error\",\"error\":{{\"code\":\"{}\",\"retryable\":false}}}}\n",
        code.as_str()
    );
    Outcome { body: body.into_bytes(), exit: 2, category: Some(code.as_str()) }
}

fn is_hex(s: &str, n: usize) -> bool {
    s.len() == n && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// Handles one request frame. `dir` is the staging directory (the cwd).
pub fn handle(request: &[u8], dir: &Path, inference: &mut dyn Inference) -> Outcome {
    let (id, doc) = match parse_frame(request) {
        Ok(v) => v,
        Err(()) => return error(ZERO_ID, Code::RequestInvalid),
    };
    match run(&id, &doc, dir, inference) {
        Ok(body) if body.len() <= MAX_RESPONSE_BYTES => {
            Outcome { body: body.into_bytes(), exit: 0, category: None }
        }
        Ok(_) => error(&id, Code::InputLimit),
        Err(code) => error(&id, code),
    }
}

/// Returns the request id (zero when unusable) and the parsed object.
fn parse_frame(request: &[u8]) -> Result<(String, Value), ()> {
    if request.len() > MAX_REQUEST_BYTES || request.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return Err(());
    }
    let text = std::str::from_utf8(request).map_err(|_| ())?;
    if !text.starts_with('{') {
        return Err(());
    }
    let (doc, used) = json::parse_prefix(text)?;
    if &text[used..] != "\n" {
        return Err(());
    }
    let id = match doc.get("request_id").and_then(Value::as_str) {
        Some(s) if is_hex(s, 32) => s.to_string(),
        _ => return Err(()),
    };
    Ok((id, doc))
}

fn exact_keys(doc: &Value, keys: &[&str]) -> bool {
    match doc.as_object() {
        Some(m) => m.len() == keys.len() && keys.iter().all(|k| m.iter().any(|(e, _)| e == k)),
        None => false,
    }
}

fn run(id: &str, doc: &Value, dir: &Path, inf: &mut dyn Inference) -> Result<String, Code> {
    match doc.get("protocol").and_then(Value::as_str) {
        Some(PROTOCOL) => {}
        Some(_) => return Err(Code::ProtocolUnsupported),
        None => return Err(Code::RequestInvalid),
    }
    let operation = doc.get("operation").and_then(Value::as_str).ok_or(Code::RequestInvalid)?;
    match operation {
        "capabilities" => {
            if !exact_keys(doc, &["protocol", "request_id", "operation"]) {
                return Err(Code::RequestInvalid);
            }
            let comp = Component::load(dir).ok_or(Code::ComponentMismatch)?;
            Ok(capabilities(id, &comp))
        }
        "embed" => {
            if !exact_keys(doc, &["protocol", "request_id", "operation", "component_id", "purpose", "items"]) {
                return Err(Code::RequestInvalid);
            }
            embed(id, doc, dir, inf)
        }
        _ => Err(Code::RequestInvalid),
    }
}

fn capabilities(id: &str, c: &Component) -> String {
    format!(
        "{{\"protocol\":\"{PROTOCOL}\",\"request_id\":\"{id}\",\"status\":\"ok\",\"component_id\":\"{}\",\"limits\":{{\"max_request_bytes\":{MAX_REQUEST_BYTES},\"max_response_bytes\":{MAX_RESPONSE_BYTES},\"max_stderr_bytes\":{MAX_STDERR_BYTES},\"max_items\":{MAX_ITEMS},\"max_total_chunks\":{MAX_TOTAL_CHUNKS},\"max_document_bytes\":{MAX_DOCUMENT_BYTES},\"max_query_bytes\":{MAX_QUERY_BYTES},\"dimensions\":{DIMENSIONS},\"max_query_tokens\":{},\"document_window_tokens\":{},\"document_overlap_tokens\":{},\"capabilities_deadline_ms\":2000,\"query_deadline_ms\":10000,\"document_deadline_ms\":60000}}}}\n",
        c.component_id, c.max_query_tokens, c.document_window_tokens, c.document_overlap_tokens
    )
}

struct Planned {
    sha: String,
    token_count: usize,
    /// (token_start, token_end, byte_start, byte_end)
    chunks: Vec<(usize, usize, usize, usize)>,
}

fn embed(id: &str, doc: &Value, dir: &Path, inf: &mut dyn Inference) -> Result<String, Code> {
    let cid = doc.get("component_id").and_then(Value::as_str).ok_or(Code::RequestInvalid)?;
    if !is_hex(cid, 64) {
        return Err(Code::RequestInvalid);
    }
    let query = match doc.get("purpose").and_then(Value::as_str) {
        Some("query") => true,
        Some("document") => false,
        _ => return Err(Code::RequestInvalid),
    };
    let items = match doc.get("items") {
        Some(Value::Array(a)) => a,
        _ => return Err(Code::RequestInvalid),
    };
    let mut texts: Vec<&str> = Vec::new();
    for (i, it) in items.iter().enumerate() {
        if !exact_keys(it, &["slot", "text"]) {
            return Err(Code::RequestInvalid);
        }
        if it.get("slot").and_then(Value::as_int) != i64::try_from(i).ok() {
            return Err(Code::RequestInvalid);
        }
        let t = it.get("text").and_then(Value::as_str).ok_or(Code::RequestInvalid)?;
        if !is_nfc(t) || t.trim().is_empty() {
            return Err(Code::RequestInvalid);
        }
        texts.push(t);
    }
    if texts.is_empty() || (query && texts.len() != 1) {
        return Err(Code::RequestInvalid);
    }
    let comp = Component::load(dir).ok_or(Code::ComponentMismatch)?;
    if comp.component_id != cid {
        return Err(Code::ComponentMismatch);
    }
    let byte_cap = if query { MAX_QUERY_BYTES } else { MAX_DOCUMENT_BYTES };
    if texts.len() > MAX_ITEMS || texts.iter().any(|t| t.len() > byte_cap) {
        return Err(Code::InputLimit);
    }

    let tok = tokenize::load(dir).map_err(|_| Code::ComponentMismatch)?;
    let prefix = if query { comp.query_prefix.as_str() } else { "" };
    let mut planned = Vec::new();
    let mut rows: Vec<Vec<u32>> = Vec::new();
    let mut total = 0usize;
    for t in &texts {
        let tokens = tokenize::tokenize(&tok, prefix, t).map_err(|e| match e {
            TokenError::Unusable => Code::ComponentMismatch,
            TokenError::Failed => Code::TokenizationFailed,
        })?;
        let n = tokens.ids.len();
        let ranges = if query {
            // The prefix is part of the model input, so it counts toward the cap.
            let prefix_tokens = tokens.prefix_ids.len();
            if n + prefix_tokens > comp.max_query_tokens {
                return Err(Code::InputLimit);
            }
            vec![(0, n)]
        } else {
            tokenize::windows(n, comp.document_window_tokens, comp.document_overlap_tokens)
        };
        total += ranges.len();
        if total > MAX_TOTAL_CHUNKS {
            return Err(Code::InputLimit);
        }
        let mut chunks = Vec::new();
        for &(a, b) in &ranges {
            let mut row = Vec::with_capacity(b - a + 2);
            row.push(comp.cls_token_id);
            row.extend_from_slice(&tokens.prefix_ids);
            row.extend_from_slice(&tokens.ids[a..b]);
            row.push(comp.sep_token_id);
            rows.push(row);
            let (bs, be) = tokenize::byte_span(&tokens.offsets, a, b);
            chunks.push((a, b, bs, be));
        }
        planned.push(Planned { sha: sha256_hex(t.as_bytes()), token_count: n, chunks });
    }

    let raw = inf.pooled(&rows).map_err(|_| Code::InferenceFailed)?;
    if raw.len() != rows.len() {
        return Err(Code::InferenceFailed);
    }
    let vectors: Vec<Vec<f32>> = raw
        .into_iter()
        .map(|v| normalize(v, comp.dimensions))
        .collect::<Result<_, _>>()?;

    let mut out = format!(
        "{{\"protocol\":\"{PROTOCOL}\",\"request_id\":\"{id}\",\"status\":\"ok\",\"component_id\":\"{}\",\"purpose\":\"{}\",\"items\":[",
        comp.component_id,
        if query { "query" } else { "document" }
    );
    let mut vi = 0;
    for (slot, p) in planned.iter().enumerate() {
        if slot > 0 {
            out.push(',');
        }
        out.push_str(&format!(
            "{{\"slot\":{slot},\"text_sha256\":{},\"token_count\":{},\"chunks\":[",
            quote(&p.sha),
            p.token_count
        ));
        for (ord, &(ts, te, bs, be)) in p.chunks.iter().enumerate() {
            if ord > 0 {
                out.push(',');
            }
            out.push_str(&format!(
                "{{\"ordinal\":{ord},\"token_start\":{ts},\"token_end\":{te},\"byte_start\":{bs},\"byte_end\":{be},\"vector\":["
            ));
            for (k, x) in vectors[vi].iter().enumerate() {
                if k > 0 {
                    out.push(',');
                }
                out.push_str(&format!("{x:e}"));
            }
            out.push_str("]}");
            vi += 1;
        }
        out.push_str("]}");
    }
    out.push_str("]}\n");
    Ok(out)
}

fn normalize(v: Vec<f32>, dims: usize) -> Result<Vec<f32>, Code> {
    if v.len() != dims || v.iter().any(|x| !x.is_finite()) {
        return Err(Code::InferenceFailed);
    }
    let norm = v.iter().map(|x| f64::from(*x) * f64::from(*x)).sum::<f64>().sqrt();
    if !norm.is_finite() || norm == 0.0 {
        return Err(Code::InferenceFailed);
    }
    let out: Vec<f32> = v
        .iter()
        .map(|x| {
            let y = (f64::from(*x) / norm) as f32;
            if y == 0.0 { 0.0 } else { y }
        })
        .collect();
    if out.iter().any(|x| !x.is_finite()) {
        return Err(Code::InferenceFailed);
    }
    Ok(out)
}
