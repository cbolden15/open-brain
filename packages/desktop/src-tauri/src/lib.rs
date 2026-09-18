mod bridge;
mod collector;
mod proof;
mod runtime;
mod strict_json;

pub use proof::run_with_paths;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    run_with_data_dir(None);
}

pub fn run_with_data_dir(data_dir: Option<std::path::PathBuf>) {
    tauri::Builder::default()
        .manage(runtime::DesktopState::new(data_dir))
        .invoke_handler(tauri::generate_handler![
            proof::run_native_proof,
            runtime::desktop_request,
            runtime::desktop_reconnect,
            runtime::desktop_reveal_managed_note,
        ])
        .build(tauri::generate_context!())
        .expect("Open Brain Desktop failed to build")
        .run(|_app, event| {
            if matches!(
                event,
                tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
            ) {
                bridge::terminate_all_owned_process_groups();
            }
        });
}
