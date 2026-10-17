/// Opaque by design: native details never cross the helper boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InferenceError;

pub trait Inference {
    /// `batch[i]` holds full token ids including CLS/SEP; returns one pooled, unnormalized
    /// vector per row (pooling already applied by the implementation).
    fn pooled(&mut self, batch: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError>;
}

/// Backend for builds without a real model: every call fails.
pub struct Unavailable;

impl Inference for Unavailable {
    fn pooled(&mut self, _batch: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
        Err(InferenceError)
    }
}

/// Deterministic, text-independent-of-model stand-in used by tests.
pub struct FakeInference;

impl Inference for FakeInference {
    fn pooled(&mut self, batch: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
        Ok(batch
            .iter()
            .map(|ids| {
                let mut h: u64 = 0xcbf29ce484222325;
                for id in ids {
                    h = (h ^ u64::from(*id)).wrapping_mul(0x100000001b3);
                }
                (0..384)
                    .map(|_| {
                        h ^= h << 13;
                        h ^= h >> 7;
                        h ^= h << 17;
                        ((h >> 40) as f32) / 8_388_608.0 - 1.0 + 0.001
                    })
                    .collect()
            })
            .collect())
    }
}
