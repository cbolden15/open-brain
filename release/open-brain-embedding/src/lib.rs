//! Open Brain embedding helper, stdio protocol v1.
//!
//! Everything except real model execution lives here; inference sits behind
//! [`Inference`] so the protocol, tokenization and windowing are testable
//! without ONNX Runtime.

pub mod component;
pub mod inference;
pub mod json;
pub mod protocol;
pub mod tokenize;

pub use inference::{FakeInference, Inference, InferenceError, Unavailable};
pub use protocol::{Outcome, handle};
