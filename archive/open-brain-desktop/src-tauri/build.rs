fn main() {
    println!(
        "cargo:rustc-env=OPEN_BRAIN_DESKTOP_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
    tauri_build::build()
}
