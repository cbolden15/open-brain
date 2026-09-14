use serde_json::json;

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--d0-proof-json") {
        let executable_directory = std::env::current_exe()
            .ok()
            .and_then(|path| path.parent().map(std::path::Path::to_path_buf));
        let result = executable_directory
            .ok_or_else(|| "runtime_unavailable".to_owned())
            .and_then(|directory| {
                let resources = directory
                    .parent()
                    .map(|contents| contents.join("Resources/binaries"))
                    .ok_or_else(|| "runtime_unavailable".to_owned())?;
                open_brain_desktop_lib::run_with_paths(
                    &resources.join("runtime/bin/open-brain"),
                    &resources.join("runtime/libexec/open-brain-graphify"),
                    &resources.join("desktop-component-manifest-v1.json"),
                )
                .map_err(|error| error.code())
            });
        match result {
            Ok(proof) => {
                println!(
                    "{}",
                    serde_json::to_string(&proof).expect("serialize proof")
                );
            }
            Err(code) => {
                println!("{}", json!({"error": {"code": code}, "ok": false}));
                std::process::exit(1);
            }
        }
        return;
    }
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let data_dir = match arguments.as_slice() {
        [] => None,
        [flag, path] if flag == "--data-dir" && std::path::Path::new(path).is_absolute() => {
            Some(std::path::PathBuf::from(path))
        }
        _ => {
            eprintln!("Usage: open-brain-desktop [--data-dir ABSOLUTE_BRAIN_PATH]");
            std::process::exit(2);
        }
    };
    open_brain_desktop_lib::run_with_data_dir(data_dir);
}
