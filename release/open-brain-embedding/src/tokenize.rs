//! Tokenization with the pinned `tokenizer.json`, plus window planning.

use std::path::Path;
use tokenizers::Tokenizer;

#[derive(Debug, PartialEq, Eq)]
pub enum TokenError {
    /// Tokenizer unavailable or not behaving as the profile requires.
    Unusable,
    /// Empty result or non-whitespace text not represented by any token.
    Failed,
}

/// Content tokens of one text: ids and byte offsets into that text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tokens {
    pub ids: Vec<u32>,
    pub offsets: Vec<(usize, usize)>,
    /// Ids of the prefix tokens that precede the content (empty without a prefix).
    pub prefix_ids: Vec<u32>,
}

pub fn load(dir: &Path) -> Result<Tokenizer, TokenError> {
    let mut t = Tokenizer::from_file(dir.join("tokenizer").join("tokenizer.json"))
        .map_err(|_| TokenError::Unusable)?;
    t.with_truncation(None).map_err(|_| TokenError::Unusable)?;
    t.with_padding(None);
    Ok(t)
}

/// Tokenizes `prefix + text` without special tokens and returns only the
/// tokens of `text`, with offsets relative to `text`. Every non-whitespace
/// character of `text` must lie inside some token's offsets.
pub fn tokenize(tok: &Tokenizer, prefix: &str, text: &str) -> Result<Tokens, TokenError> {
    let full = format!("{prefix}{text}");
    let plen = prefix.len();
    let enc = tok.encode(full.as_str(), false).map_err(|_| TokenError::Failed)?;
    let mut ids = Vec::new();
    let mut offsets = Vec::new();
    let mut prefix_ids = Vec::new();
    let mut covered = vec![false; text.len()];
    for (id, &(s, e)) in enc.get_ids().iter().zip(enc.get_offsets()) {
        if s > e || e > full.len() || !full.is_char_boundary(s) || !full.is_char_boundary(e) {
            return Err(TokenError::Failed);
        }
        if plen > 0 && e <= plen {
            prefix_ids.push(*id);
            continue;
        }
        if s < plen {
            return Err(TokenError::Failed); // token straddles the prefix boundary
        }
        let (s, e) = (s - plen, e - plen);
        for c in &mut covered[s..e] {
            *c = true;
        }
        ids.push(*id);
        offsets.push((s, e));
    }
    if ids.is_empty() {
        return Err(TokenError::Failed);
    }
    for (i, c) in text.char_indices() {
        if !c.is_whitespace() && !covered[i] {
            return Err(TokenError::Failed);
        }
    }
    Ok(Tokens { ids, offsets, prefix_ids })
}

/// Half-open token ranges: `[0, min(w, n))`, advance by `w - overlap`, stop
/// when the previous end equals `n`.
pub fn windows(n: usize, window: usize, overlap: usize) -> Vec<(usize, usize)> {
    let stride = window - overlap;
    let mut out = Vec::new();
    let mut start = 0;
    loop {
        let end = (start + window).min(n);
        out.push((start, end));
        if end == n {
            return out;
        }
        start += stride;
    }
}

/// Minimal byte span covering the offsets of tokens `[a, b)`.
pub fn byte_span(offsets: &[(usize, usize)], a: usize, b: usize) -> (usize, usize) {
    let slice = &offsets[a..b];
    let start = slice.iter().map(|o| o.0).min().unwrap_or(0);
    let end = slice.iter().map(|o| o.1).max().unwrap_or(0);
    (start, end)
}
