use open_brain_embedding::json::{self, Value};
use open_brain_embedding::tokenize::windows;
use open_brain_embedding::{FakeInference, Inference, InferenceError, handle};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const ID: &str = "0123456789abcdef0123456789abcdef";

fn crate_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn fixtures() -> PathBuf {
    crate_dir().join("tests/fixtures")
}

fn tokenizer_asset() -> Option<PathBuf> {
    let p = crate_dir().join("../../build/g4-assets-1/minilm/tokenizer.json");
    if p.exists() {
        Some(p)
    } else {
        eprintln!("SKIP: {} is absent (run the G4 asset fetch first)", p.display());
        None
    }
}

fn stage(name: &str, component: Option<&[u8]>) -> Option<PathBuf> {
    let tok = tokenizer_asset()?;
    let dir = std::env::temp_dir().join(format!("ob-embed-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(dir.join("tokenizer")).unwrap();
    fs::copy(tok, dir.join("tokenizer/tokenizer.json")).unwrap();
    if let Some(c) = component {
        fs::write(dir.join("component.json"), c).unwrap();
    }
    Some(dir)
}

fn default_component() -> Vec<u8> {
    fs::read(fixtures().join("component.json")).unwrap()
}

/// Replaces every vector array with a placeholder after checking its shape.
fn mask_vectors(out: &str) -> String {
    let mut res = String::new();
    let mut rest = out;
    while let Some(i) = rest.find("\"vector\":[") {
        let start = i + "\"vector\":[".len();
        res.push_str(&rest[..i]);
        let end = start + rest[start..].find(']').unwrap();
        let nums: Vec<f32> = rest[start..end].split(',').map(|n| n.parse().unwrap()).collect();
        assert_eq!(nums.len(), 384);
        let norm: f64 = nums.iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
        assert!((norm - 1.0).abs() < 1e-5, "norm {norm}");
        res.push_str("\"vector\":\"<384>\"");
        rest = &rest[end + 1..];
    }
    res.push_str(rest);
    res
}

#[test]
fn golden_fixtures() {
    let Some(dir) = stage("golden", Some(&default_component())) else { return };
    let mut names: Vec<String> = fs::read_dir(fixtures())
        .unwrap()
        .filter_map(|e| e.unwrap().file_name().into_string().ok())
        .filter_map(|n| n.strip_suffix(".request").map(str::to_string))
        .collect();
    names.sort();
    assert!(names.len() >= 20, "fixtures missing: {names:?}");
    for n in names {
        let req = fs::read(fixtures().join(format!("{n}.request"))).unwrap();
        let want = fs::read_to_string(fixtures().join(format!("{n}.response"))).unwrap();
        let out = handle(&req, &dir, &mut FakeInference);
        let body = String::from_utf8(out.body).unwrap();
        assert_eq!(mask_vectors(&body), want, "fixture {n}");
        assert!(body.ends_with('\n') && !body[..body.len() - 1].contains('\n'), "{n}");
        assert_eq!(out.exit, if body.contains("\"status\":\"ok\"") { 0 } else { 2 }, "{n}");
        assert_eq!(out.category.is_some(), out.exit == 2, "{n}");
    }
}

fn code_of(body: &[u8]) -> String {
    let v = json::parse_exact(std::str::from_utf8(body).unwrap().trim_end()).unwrap();
    match v.get("error").and_then(|e| e.get("code")) {
        Some(Value::Str(s)) => s.clone(),
        _ => "ok".to_string(),
    }
}

fn query_req(cid: &str, text: &str) -> Vec<u8> {
    format!(
        "{{\"protocol\":\"open-brain.embedding-helper.v1\",\"request_id\":\"{ID}\",\"operation\":\"embed\",\"component_id\":\"{cid}\",\"purpose\":\"query\",\"items\":[{{\"slot\":0,\"text\":\"{text}\"}}]}}\n"
    )
    .into_bytes()
}

#[test]
fn missing_component_fails_every_operation() {
    let Some(dir) = stage("nocomp", None) else { return };
    for n in ["capabilities_ok", "query_ok"] {
        let req = fs::read(fixtures().join(format!("{n}.request"))).unwrap();
        let out = handle(&req, &dir, &mut FakeInference);
        assert_eq!(out.exit, 2);
        assert_eq!(code_of(&out.body), "component_mismatch", "{n}");
    }
}

#[test]
fn unusable_component_is_mismatch() {
    let Some(dir) = stage("badcomp", Some(b"{\"preprocess\":{\"dimensions\":768}}")) else { return };
    let req = fs::read(fixtures().join("capabilities_ok.request")).unwrap();
    assert_eq!(code_of(&handle(&req, &dir, &mut FakeInference).body), "component_mismatch");
}

#[test]
fn oversize_request_is_invalid_with_zero_id() {
    let Some(dir) = stage("big", Some(&default_component())) else { return };
    let out = handle(&vec![b' '; 131_073], &dir, &mut FakeInference);
    assert_eq!(code_of(&out.body), "request_invalid");
    assert!(String::from_utf8(out.body).unwrap().contains(&"0".repeat(32)));
}

#[test]
fn query_prefix_is_model_input_but_not_counted() {
    let mut comp = String::from_utf8(default_component()).unwrap();
    comp = comp.replace("\"query_prefix\":\"\"", "\"query_prefix\":\"query: \"");
    let Some(dir) = stage("prefix", Some(comp.as_bytes())) else { return };
    let cid = open_brain_embedding::component::sha256_hex(comp.as_bytes());
    struct Spy(Vec<Vec<u32>>);
    impl Inference for Spy {
        fn pooled(&mut self, b: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
            self.0 = b.to_vec();
            FakeInference.pooled(b)
        }
    }
    let mut spy = Spy(vec![]);
    let out = handle(&query_req(&cid, "hello world"), &dir, &mut spy);
    let body = String::from_utf8(out.body).unwrap();
    assert!(body.contains("\"token_count\":2,"), "{body:.300}");
    assert!(body.contains("\"byte_start\":0,\"byte_end\":11"), "{body:.300}");
    // [CLS] query : hello world [SEP]
    assert_eq!(spy.0[0].len(), 2 + 2 + 2);
    assert_eq!((spy.0[0][0], *spy.0[0].last().unwrap()), (101, 102));
}

#[test]
fn bad_inference_output_is_inference_failed() {
    let Some(dir) = stage("badinf", Some(&default_component())) else { return };
    let req = fs::read(fixtures().join("query_ok.request")).unwrap();
    for bad in [
        vec![f32::NAN; 384],
        vec![f32::INFINITY; 384],
        vec![0.0; 384],
        vec![1.0; 383],
    ] {
        struct Bad(Vec<f32>);
        impl Inference for Bad {
            fn pooled(&mut self, b: &[Vec<u32>]) -> Result<Vec<Vec<f32>>, InferenceError> {
                Ok(b.iter().map(|_| self.0.clone()).collect())
            }
        }
        let out = handle(&req, &dir, &mut Bad(bad));
        assert_eq!(code_of(&out.body), "inference_failed");
    }
    let out = handle(&req, &dir, &mut open_brain_embedding::Unavailable);
    assert_eq!(code_of(&out.body), "inference_failed");
}

#[test]
fn vectors_round_trip_f32_exactly() {
    let Some(dir) = stage("rt", Some(&default_component())) else { return };
    let req = fs::read(fixtures().join("query_ok.request")).unwrap();
    let body = String::from_utf8(handle(&req, &dir, &mut FakeInference).body).unwrap();
    let doc = json::parse_exact(body.trim_end()).unwrap();
    let Value::Array(items) = doc.get("items").unwrap() else { panic!() };
    let Value::Array(chunks) = items[0].get("chunks").unwrap() else { panic!() };
    let Value::Array(vec) = chunks[0].get("vector").unwrap() else { panic!() };
    // Recompute the expected normalized vector and compare bits.
    let raw = FakeInference.pooled(&[{
        // ids come from the real tokenizer; recover them via a spy-free route
        let tok = open_brain_embedding::tokenize::load(&dir).unwrap();
        let t = open_brain_embedding::tokenize::tokenize(&tok, "", "Where did we discuss local memory?").unwrap();
        let mut r = vec![101];
        r.extend(t.ids);
        r.push(102);
        r
    }])
    .unwrap();
    let norm = raw[0].iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
    for (i, v) in vec.iter().enumerate() {
        let got: f32 = match v {
            Value::Float(f) => *f as f32,
            Value::Int(n) => *n as f32,
            _ => panic!(),
        };
        let want = (f64::from(raw[0][i]) / norm) as f32;
        assert_eq!(got.to_bits(), want.to_bits(), "component {i}");
    }
}

#[test]
fn offsets_are_utf8_byte_offsets() {
    let Some(dir) = stage("offs", Some(&default_component())) else { return };
    let tok = open_brain_embedding::tokenize::load(&dir).unwrap();
    let text = "naïve 日本 café 😀";
    let t = open_brain_embedding::tokenize::tokenize(&tok, "", text);
    // The emoji is either an unknown token covering its 4 bytes, or the item fails.
    let t = t.expect("emoji maps to [UNK] with offsets");
    for &(s, e) in &t.offsets {
        assert!(text.is_char_boundary(s) && text.is_char_boundary(e) && s < e);
    }
    assert_eq!(t.offsets[0], (0, 6)); // "naïve": ï is two bytes
    assert_eq!(*t.offsets.last().unwrap(), (text.len() - 4, text.len()));
}

#[test]
fn window_plan_matches_spec() {
    assert_eq!(windows(1, 224, 32), vec![(0, 1)]);
    assert_eq!(windows(224, 224, 32), vec![(0, 224)]);
    assert_eq!(windows(225, 224, 32), vec![(0, 224), (192, 225)]);
    assert_eq!(windows(416, 224, 32), vec![(0, 224), (192, 416)]);
    assert_eq!(windows(417, 224, 32), vec![(0, 224), (192, 416), (384, 417)]);
}

#[test]
fn strict_json_rejections() {
    for bad in [
        "{\"a\":1,\"a\":2}",
        "[[[[[[[[[1]]]]]]]]]",
        "\"\\ud800\"",
        "{\"a\":NaN}",
        "{\"a\":01}",
        "\"a\u{1}b\"",
        "{} {}",
    ] {
        assert!(json::parse_exact(bad).is_err(), "{bad}");
    }
    assert!(json::parse_exact("[[[[[[[1]]]]]]]").is_ok());
    assert!(json::parse_exact("[[[[[[[[1]]]]]]]]").is_ok(), "depth 8 is the inclusive limit");
    assert_eq!(json::parse_exact("true").unwrap(), Value::Bool(true));
    assert_ne!(json::parse_exact("true").unwrap().as_int(), Some(1));
    assert_eq!(json::parse_exact("\"\\ud83d\\ude00\"").unwrap(), Value::Str("😀".into()));
}

fn run_bin(args: &[&str], cwd: &Path, input: &[u8]) -> (i32, Vec<u8>, Vec<u8>) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_open-brain-embedding"))
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let data = input.to_vec();
    let w = std::thread::spawn(move || {
        let _ = stdin.write_all(&data);
    });
    let out = child.wait_with_output().unwrap();
    w.join().unwrap();
    (out.status.code().unwrap(), out.stdout, out.stderr)
}

#[test]
fn binary_bad_argv_exits_2_without_frame() {
    let Some(dir) = stage("argv", Some(&default_component())) else { return };
    let req = fs::read(fixtures().join("capabilities_ok.request")).unwrap();
    for args in [&[][..], &["--stdio-v2"], &["--stdio-v1", "x"], &["x"]] {
        let (code, out, err) = run_bin(args, &dir, &req);
        assert_eq!((code, out.len()), (2, 0), "{args:?}");
        assert!(err.len() <= 4096);
    }
}

#[test]
fn binary_capabilities_and_errors_end_to_end() {
    let Some(dir) = stage("bin", Some(&default_component())) else { return };
    let cap = fs::read(fixtures().join("capabilities_ok.request")).unwrap();
    let (code, out, err) = run_bin(&["--stdio-v1"], &dir, &cap);
    assert_eq!(code, 0);
    assert!(err.is_empty());
    let want = fs::read_to_string(fixtures().join("capabilities_ok.response")).unwrap();
    assert_eq!(String::from_utf8(out).unwrap(), want);

    let bad = fs::read(fixtures().join("duplicate_key.request")).unwrap();
    let (code, out, err) = run_bin(&["--stdio-v1"], &dir, &bad);
    assert_eq!(code, 2);
    assert_eq!(err, b"request_invalid\n");
    assert_eq!(String::from_utf8(out).unwrap(), fs::read_to_string(fixtures().join("duplicate_key.response")).unwrap());

    let (code, out, _) = run_bin(&["--stdio-v1"], &dir, b"");
    assert_eq!(code, 2);
    assert_eq!(code_of(&out), "request_invalid");
}

#[cfg(feature = "fake-inference")]
#[test]
fn binary_embed_end_to_end_with_fake_backend() {
    let Some(dir) = stage("binfake", Some(&default_component())) else { return };
    for n in ["query_ok", "document_windows_ok"] {
        let req = fs::read(fixtures().join(format!("{n}.request"))).unwrap();
        let (code, out, err) = run_bin(&["--stdio-v1"], &dir, &req);
        assert_eq!((code, err.len()), (0, 0), "{n}");
        let want = fs::read_to_string(fixtures().join(format!("{n}.response"))).unwrap();
        assert_eq!(mask_vectors(&String::from_utf8(out).unwrap()), want, "{n}");
    }
}
