//! Native-vs-reference parity for the ONNX Runtime backend.
//!
//! Needs the local G4 assets, the development runtime and the isolated reference
//! interpreter, so it is ignored by default:
//! `cargo test --manifest-path release/open-brain-embedding/Cargo.toml --test parity -- --ignored`

#![cfg(not(feature = "fake-inference"))]

use open_brain_embedding::json::{self, Value, quote};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const ID: &str = "0123456789abcdef0123456789abcdef";

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn required(rel: &str) -> PathBuf {
    let p = repo().join(rel);
    assert!(p.exists(), "{} is absent", p.display());
    p
}

fn run_helper(dir: &Path, input: &[u8]) -> (i32, Vec<u8>, Vec<u8>) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_open-brain-embedding"))
        .arg("--stdio-v1")
        .current_dir(dir)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    let out = child.wait_with_output().unwrap();
    (out.status.code().unwrap(), out.stdout, out.stderr)
}

fn num(v: &Value) -> f64 {
    match v {
        Value::Float(f) => *f,
        Value::Int(n) => *n as f64,
        other => panic!("not a number: {other:?}"),
    }
}

fn request(cid: &str, purpose: &str, texts: &[String]) -> Vec<u8> {
    let items: Vec<String> = texts
        .iter()
        .enumerate()
        .map(|(i, t)| format!("{{\"slot\":{i},\"text\":{}}}", quote(t)))
        .collect();
    format!(
        "{{\"protocol\":\"open-brain.embedding-helper.v1\",\"request_id\":\"{ID}\",\"operation\":\"embed\",\"component_id\":\"{cid}\",\"purpose\":\"{purpose}\",\"items\":[{}]}}\n",
        items.join(",")
    )
    .into_bytes()
}

/// 8 queries and 12 documents (3 requests of 4); the first document spans 3 windows.
fn requests() -> Vec<(&'static str, Vec<String>)> {
    let queries = [
        "Where did we discuss local memory?",
        "invoice reconciliation checklist",
        "Café opening hours in Zürich",
        "how do I rotate the backup key",
        "naïve Bayes spam filter notes",
        "meeting notes 2026-10-01 roadmap",
        "日本 travel itinerary",
        "What is the sensor calibration drift tolerance?",
    ];
    let long: String = (0..40)
        .map(|i| format!("Synthetic ledger entry {i} records a calibrated sensor reading. "))
        .collect();
    let docs = [
        long,
        "The quarterly review covered hiring, budget and the migration timeline.".to_string(),
        "Grocery list: oat milk, rye bread, two lemons, and coffee beans.".to_string(),
        "Résumé draft — sections: experience, education, publications.".to_string(),
        "A short note.".to_string(),
        "Backup keys rotate every ninety days; the old key is retired after verification."
            .to_string(),
        "Emoji check 😀 and symbols ± × ÷ in running text.".to_string(),
        "Project Falcon kickoff: owners, milestones, risks and open questions.".to_string(),
        "Reading list:\n- Distributed systems\n- Information retrieval\n- Typography".to_string(),
        "The cat sat on the mat while the kettle boiled.".to_string(),
        "Recipe: whisk eggs, fold in flour, bake at 180 degrees for 25 minutes.".to_string(),
        "Call notes with the landlord about the heating repair schedule.".to_string(),
    ];
    let mut out: Vec<(&'static str, Vec<String>)> =
        queries.iter().map(|q| ("query", vec![q.to_string()])).collect();
    for group in docs.chunks(4) {
        out.push(("document", group.to_vec()));
    }
    out
}

#[test]
#[ignore = "needs local G4 assets, runtime and reference interpreter"]
fn ort_backend_matches_reference_embedder() {
    let dylib = required("build/g4-ort-macos/Release/libonnxruntime.1.31.0.dylib");
    let assets = required("build/g4-assets-1/minilm");
    let python = required("build/g4-ref/bin/python");
    let reference = required("tools/g4_embedding_probe/payload/reference.py.txt");
    let tmp = std::env::temp_dir().join(format!("ob-embed-parity-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).unwrap();
    let stage = tmp.join("stage");
    let status = Command::new(&python)
        .arg("-I")
        .arg(required("tools/g4_embedding_probe/stage.py"))
        .arg("--output")
        .arg(&stage)
        .arg("--helper")
        .arg(env!("CARGO_BIN_EXE_open-brain-embedding"))
        .arg("--runtime-library")
        .arg(&dylib)
        .arg("--assets")
        .arg(&assets)
        .stdout(Stdio::null())
        .status()
        .unwrap();
    assert!(status.success(), "staging failed");
    let cid = open_brain_embedding::component::sha256_hex(&fs::read(stage.join("component.json")).unwrap());

    // Native rows: [token_count, token_start, token_end, byte_start, byte_end, *vector].
    let reqs = requests();
    let mut native: Vec<Vec<f64>> = Vec::new();
    let mut three_windows = false;
    for (purpose, texts) in &reqs {
        let (code, out, err) = run_helper(&stage, &request(&cid, purpose, texts));
        assert_eq!(code, 0, "{}", String::from_utf8_lossy(&out));
        assert!(err.is_empty(), "stderr must stay empty on success");
        let doc = json::parse_exact(std::str::from_utf8(&out).unwrap().trim_end()).unwrap();
        let Some(Value::Array(items)) = doc.get("items") else { panic!("no items") };
        for item in items {
            let n = num(item.get("token_count").unwrap());
            let Some(Value::Array(chunks)) = item.get("chunks") else { panic!("no chunks") };
            three_windows |= chunks.len() == 3;
            for c in chunks {
                let mut row = vec![n];
                for k in ["token_start", "token_end", "byte_start", "byte_end"] {
                    row.push(num(c.get(k).unwrap()));
                }
                let Some(Value::Array(v)) = c.get("vector") else { panic!("no vector") };
                row.extend(v.iter().map(num));
                native.push(row);
            }
        }
    }
    assert!(three_windows, "one document must span 3 windows");

    let input: Vec<String> = reqs
        .iter()
        .map(|(p, texts)| {
            let t: Vec<String> = texts.iter().map(|t| quote(t)).collect();
            format!("{{\"purpose\":\"{p}\",\"texts\":[{}]}}", t.join(","))
        })
        .collect();
    let input_path = tmp.join("input.json");
    let output_path = tmp.join("reference.json");
    fs::write(&input_path, format!("[{}]", input.join(","))).unwrap();
    let status = Command::new(&python)
        .arg("-I")
        .arg(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/parity_reference.py"))
        .args([&reference, &assets, &input_path, &output_path])
        .status()
        .unwrap();
    assert!(status.success(), "reference driver failed");
    let text = fs::read_to_string(&output_path).unwrap();
    let Value::Array(expected) = json::parse_exact(&text).unwrap() else { panic!() };

    assert_eq!(native.len(), expected.len(), "chunk count");
    let (mut worst_cos, mut worst_abs) = (1.0f64, 0.0f64);
    for (i, (got, want)) in native.iter().zip(&expected).enumerate() {
        let Value::Array(want) = want else { panic!() };
        let want: Vec<f64> = want.iter().map(num).collect();
        assert_eq!(got.len(), 5 + 384, "chunk {i}");
        assert_eq!(got[..5], want[..5], "chunk {i} token/byte ranges");
        let (a, b) = (&got[5..], &want[5..]);
        let dot: f64 = a.iter().zip(b).map(|(x, y)| x * y).sum();
        let na = a.iter().map(|x| x * x).sum::<f64>().sqrt();
        let nb = b.iter().map(|x| x * x).sum::<f64>().sqrt();
        let cos = dot / (na * nb);
        let max_abs = a.iter().zip(b).map(|(x, y)| (x - y).abs()).fold(0.0, f64::max);
        assert!(cos >= 0.99999, "chunk {i}: cosine {cos}");
        assert!(max_abs <= 1e-5, "chunk {i}: max abs diff {max_abs}");
        worst_cos = worst_cos.min(cos);
        worst_abs = worst_abs.max(max_abs);
    }
    eprintln!(
        "PARITY chunks={} min_cosine={worst_cos:.9} max_abs_diff={worst_abs:.3e}",
        native.len()
    );

    // A staged runtime without a model fails closed, text-free.
    let nomodel = tmp.join("nomodel");
    fs::create_dir_all(nomodel.join("tokenizer")).unwrap();
    fs::create_dir_all(nomodel.join("runtime")).unwrap();
    fs::copy(stage.join("component.json"), nomodel.join("component.json")).unwrap();
    fs::copy(stage.join("tokenizer/tokenizer.json"), nomodel.join("tokenizer/tokenizer.json")).unwrap();
    let runtime = open_brain_embedding::ort_inference::RUNTIME_NAME;
    std::os::unix::fs::symlink(stage.join("runtime").join(runtime), nomodel.join("runtime").join(runtime)).unwrap();
    let (code, out, err) = run_helper(&nomodel, &request(&cid, "query", &["hello".to_string()]));
    assert_eq!(code, 2);
    assert!(String::from_utf8(out).unwrap().contains("\"code\":\"inference_failed\""));
    assert_eq!(err, b"inference_failed\n");

    fs::remove_dir_all(&tmp).unwrap();
}
