//! Open Brain embedding helper, stdio protocol v1.
//!
//! Inference sits behind [`Inference`] so the protocol, tokenization and
//! windowing are testable without ONNX Runtime; [`OrtInference`] is the real,
//! statically linked backend.

pub mod component;
pub mod inference;
pub mod json;
pub mod ort_inference;
pub mod protocol;
pub mod tokenize;

pub use inference::{FakeInference, Inference, InferenceError, Unavailable};
pub use ort_inference::OrtInference;
pub use protocol::{Outcome, handle};
