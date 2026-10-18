use crate::json;
use sha2::{Digest, Sha256};
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pooling {
    Mean,
    Cls,
}

#[derive(Debug, Clone)]
pub struct Component {
    pub component_id: String,
    pub profile_id: String,
    pub dimensions: usize,
    pub pooling: Pooling,
    pub max_query_tokens: usize,
    pub document_window_tokens: usize,
    pub document_overlap_tokens: usize,
    pub cls_token_id: u32,
    pub sep_token_id: u32,
    pub pad_token_id: u32,
    pub query_prefix: String,
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

impl Component {
    /// Reads `component.json` from `dir`. `None` means missing, unreadable or
    /// not matching the approved v1 profile shape.
    pub fn load(dir: &Path) -> Option<Component> {
        let bytes = std::fs::read(dir.join("component.json")).ok()?;
        let text = std::str::from_utf8(&bytes).ok()?;
        let doc = json::parse_exact(text.trim_end_matches(['\n', '\r', ' ', '\t'])).ok()?;
        let pre = doc.get("preprocess")?;
        let int = |k: &str| -> Option<u64> { u64::try_from(pre.get(k)?.as_int()?).ok() };
        let tok = |k: &str| -> Option<u32> { u32::try_from(int(k)?).ok() };
        let pooling = match pre.get("pooling")?.as_str()? {
            "mean" => Pooling::Mean,
            "cls" => Pooling::Cls,
            _ => return None,
        };
        let c = Component {
            component_id: sha256_hex(&bytes),
            profile_id: pre.get("profile_id")?.as_str()?.to_string(),
            dimensions: usize::try_from(int("dimensions")?).ok()?,
            pooling,
            max_query_tokens: usize::try_from(int("max_query_tokens")?).ok()?,
            document_window_tokens: usize::try_from(int("document_window_tokens")?).ok()?,
            document_overlap_tokens: usize::try_from(int("document_overlap_tokens")?).ok()?,
            cls_token_id: tok("cls_token_id")?,
            sep_token_id: tok("sep_token_id")?,
            pad_token_id: tok("pad_token_id")?,
            query_prefix: pre.get("query_prefix")?.as_str()?.to_string(),
        };
        let fixed = c.dimensions == 384
            && c.max_query_tokens == 254
            && c.document_window_tokens == 224
            && c.document_overlap_tokens == 32;
        fixed.then_some(c)
    }
}
