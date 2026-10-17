use open_brain_embedding::{Inference, handle};
use std::io::{Read, Write};
use std::path::Path;
use std::process::ExitCode;

#[cfg(feature = "fake-inference")]
fn backend() -> impl Inference {
    open_brain_embedding::FakeInference
}

#[cfg(not(feature = "fake-inference"))]
fn backend() -> impl Inference {
    open_brain_embedding::OrtInference::new(Path::new("."))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args != ["--stdio-v1"] {
        eprintln!("bad_argv");
        return ExitCode::from(2);
    }
    let mut buf = Vec::new();
    // One byte past the cap is enough to prove the request is oversize.
    let cap = open_brain_embedding::protocol::MAX_REQUEST_BYTES as u64 + 1;
    if std::io::stdin().lock().take(cap).read_to_end(&mut buf).is_err() {
        buf.clear();
    }
    let out = handle(&buf, Path::new("."), &mut backend());
    let mut so = std::io::stdout().lock();
    if so.write_all(&out.body).and_then(|_| so.flush()).is_err() {
        return ExitCode::from(1);
    }
    if let Some(c) = out.category {
        eprintln!("{c}");
    }
    ExitCode::from(out.exit as u8)
}
