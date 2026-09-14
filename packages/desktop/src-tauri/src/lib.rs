mod bridge;
mod proof;

pub use proof::run_with_paths;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![proof::run_native_proof])
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
