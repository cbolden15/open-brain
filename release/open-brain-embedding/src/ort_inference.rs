//! ONNX Runtime backend. ONNX Runtime is linked statically into the helper, so nothing
//! is loaded from the staged directory except the model; no loader environment variable is read.

use crate::component::{Component, Pooling};
use crate::inference::{Inference, InferenceError};
use ort::logging::LogLevel;
use ort::session::Session;
use ort::session::builder::AutoDevicePolicy;
use ort::value::Tensor;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Rows per session run, as in the reference embedder's document batches.
const BATCH_ROWS: usize = 16;

pub struct OrtInference {
    dir: PathBuf,
    state: State,
}

enum State {
    Unloaded,
    Ready(Loaded),
    Failed,
}

struct Loaded {
    session: Session,
    pooling: Pooling,
    pad: i64,
    token_types: bool,
}

impl OrtInference {
    /// Nothing is loaded until the first [`Inference::pooled`] call.
    pub fn new(dir: &Path) -> OrtInference {
        OrtInference { dir: dir.to_path_buf(), state: State::Unloaded }
    }
}

fn load(dir: &Path) -> Option<Loaded> {
    let dir = std::path::absolute(dir).ok()?;
    let comp = Component::load(&dir)?;
    let env = ort::init()
        .with_name("open-brain-embedding")
        .with_telemetry(false)
        // Native log text never reaches stderr.
        .with_logger(Arc::new(|_, _, _, _, _| {}))
        .build()
        .ok()?;
    env.set_log_level(LogLevel::Fatal);
    let session = Session::builder(&env)
        .ok()?
        .with_auto_device(AutoDevicePolicy::PreferCPU)
        .ok()?
        .with_intra_threads(2)
        .ok()?
        .with_inter_threads(1)
        .ok()?
        .with_parallel_execution(false)
        .ok()?
        .with_log_level(LogLevel::Fatal)
        .ok()?
        .commit_from_file(dir.join("model").join("model.onnx"))
        .ok()?;
    let has = |name: &str| session.inputs().iter().any(|i| i.name() == name);
    if !has("input_ids") || !has("attention_mask") {
        return None;
    }
    if !session.outputs().iter().any(|o| o.name() == "last_hidden_state") {
        return None;
    }
    let token_types = has("token_type_ids");
    Some(Loaded { session, pooling: comp.pooling, pad: i64::from(comp.pad_token_id), token_types })
}

impl Loaded {
    fn run(&mut self, rows: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
        let n = rows.len();
        let width = rows.iter().map(Vec::len).max().unwrap_or(0);
        if n == 0 || width == 0 {
            return Err(InferenceError);
        }
        let mut ids = vec![self.pad; n * width];
        let mut mask = vec![0i64; n * width];
        for (i, row) in rows.iter().enumerate() {
            for (j, &t) in row.iter().enumerate() {
                ids[i * width + j] = i64::from(t);
                mask[i * width + j] = 1;
            }
        }
        let shape = [n, width];
        let tensor = |data: Vec<i64>| Tensor::from_array((shape, data)).map_err(|_| InferenceError);
        let mut inputs = ort::inputs![
            "input_ids" => tensor(ids)?,
            "attention_mask" => tensor(mask)?,
        ];
        if self.token_types {
            inputs.push(("token_type_ids".into(), tensor(vec![0; n * width])?.into()));
        }
        let outputs = self.session.run(inputs).map_err(|_| InferenceError)?;
        let (dims, states) = outputs
            .get("last_hidden_state")
            .ok_or(InferenceError)?
            .try_extract_tensor::<f32>()
            .map_err(|_| InferenceError)?;
        let hidden = match **dims {
            [b, s, h] if b == n as i64 && s == width as i64 && h > 0 => h as usize,
            _ => return Err(InferenceError),
        };
        if states.len() != n * width * hidden {
            return Err(InferenceError);
        }
        let state = |i: usize, j: usize| &states[(i * width + j) * hidden..][..hidden];
        Ok((0..n)
            .map(|i| match self.pooling {
                Pooling::Cls => state(i, 0).to_vec(),
                Pooling::Mean => {
                    // Masked mean over real tokens; padding positions are excluded.
                    let mut sum = vec![0f64; hidden];
                    let real = rows[i].len();
                    for j in 0..real {
                        for (s, x) in sum.iter_mut().zip(state(i, j)) {
                            *s += f64::from(*x);
                        }
                    }
                    sum.iter().map(|s| (s / real as f64) as f32).collect()
                }
            })
            .collect())
    }
}

impl Inference for OrtInference {
    fn pooled(&mut self, batch: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
        if matches!(self.state, State::Unloaded) {
            self.state = load(&self.dir).map_or(State::Failed, State::Ready);
        }
        let State::Ready(loaded) = &mut self.state else { return Err(InferenceError) };
        let mut out = Vec::with_capacity(batch.len());
        for rows in batch.chunks(BATCH_ROWS) {
            out.extend(loaded.run(rows)?);
        }
        Ok(out)
    }
}
