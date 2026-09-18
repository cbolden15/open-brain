use crate::bridge::{Bridge, BridgeError, PROTOCOL, PROTOCOL_VERSION};
use crate::collector::{CollectorConnection, OPERATIONS as SOURCE_OPERATIONS};
use crate::proof::validate_runtime_pair;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager, State, path::BaseDirectory};

const SUPPORTED_STATE_SCHEMA: u64 = 7;
const SUPPORTED_RUNTIME_SESSION: u64 = 2;
const BASE_OPERATIONS: &[&str] = &[
    "system.status",
    "capture.create",
    "search.query",
    "agent.setup.preview",
    "agent.setup.apply",
    "contract.describe",
    "inbox.list",
    "inbox.route",
    "space.create",
    "space.list",
    "space.rename",
    "publication.approve",
    "publication.edit_and_approve",
    "publication.list",
    "publication.propose",
    "publication.reject",
    "publication.show",
    "workspace.refresh",
    "workspace.setup",
    "workspace.status",
];
const NEGOTIATED_OPERATIONS: &[&str] = &["search.page", "record.read"];
const COLLECTOR_OPERATIONS: &[&str] = &[
    "collector.enable",
    "collector.schedule",
    "collector.disable",
    "collector.pause",
    "collector.resume",
    "collector.status",
    "collector.sync_now",
];

#[derive(Default)]
pub(crate) struct DesktopState {
    session: Arc<Mutex<Option<Bridge>>>,
    collector: Arc<Mutex<CollectorConnection>>,
    approved_pages: Arc<Mutex<HashSet<String>>>,
    managed_notes: Arc<Mutex<Option<ManagedNotes>>>,
    data_dir: Option<PathBuf>,
}

#[derive(Debug)]
struct ManagedNotes {
    wire_vault: PathBuf,
    canonical_vault: PathBuf,
    relative_paths: HashSet<PathBuf>,
}

impl DesktopState {
    pub fn new(data_dir: Option<PathBuf>) -> Self {
        Self {
            session: Arc::default(),
            collector: Arc::default(),
            approved_pages: Arc::default(),
            managed_notes: Arc::default(),
            data_dir,
        }
    }
}

#[tauri::command]
pub(crate) async fn desktop_request(
    app: AppHandle,
    state: State<'_, DesktopState>,
    operation: String,
    arguments: Value,
    request_id: Option<String>,
) -> Result<Value, String> {
    if !(BASE_OPERATIONS.contains(&operation.as_str())
        || NEGOTIATED_OPERATIONS.contains(&operation.as_str())
        || COLLECTOR_OPERATIONS.contains(&operation.as_str())
        || SOURCE_OPERATIONS.contains(&operation.as_str()))
        || !arguments.is_object()
    {
        return Err("invalid_request".to_owned());
    }
    let session = Arc::clone(&state.session);
    let collector = Arc::clone(&state.collector);
    let approved_pages = Arc::clone(&state.approved_pages);
    let managed_notes = Arc::clone(&state.managed_notes);
    let data_dir = state.data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || {
        if SOURCE_OPERATIONS.contains(&operation.as_str()) {
            let brain = {
                let mut guard = session
                    .try_lock()
                    .map_err(|_| "operation_in_progress".to_owned())?;
                if guard.is_none() {
                    *guard = Some(connect(&app, data_dir.as_deref())?);
                }
                let status = guard
                    .as_mut()
                    .ok_or("bridge_closed")?
                    .invoke("system.status", json!({}), None, Duration::from_secs(10))
                    .map_err(|error| error.code())?;
                PathBuf::from(
                    status
                        .get("brain_root")
                        .and_then(Value::as_str)
                        .ok_or("source_brain_unavailable")?,
                )
            };
            return collector
                .try_lock()
                .map_err(|_| "operation_in_progress".to_owned())?
                .request(&brain, &operation, arguments);
        }
        let mut guard = session
            .try_lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
        if guard.is_none() {
            *guard = Some(connect(&app, data_dir.as_deref())?);
        }
        let bridge = guard.as_mut().ok_or_else(|| "bridge_closed".to_owned())?;
        let response = bridge.invoke(&operation, arguments, request_id, Duration::from_secs(10));
        match response {
            Ok(value) => {
                if matches!(
                    operation.as_str(),
                    "publication.approve" | "publication.edit_and_approve"
                ) {
                    let page_id = value
                        .get("page_id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| "malformed_response".to_owned())?;
                    approved_pages
                        .lock()
                        .map_err(|_| "operation_in_progress".to_owned())?
                        .insert(page_id.to_owned());
                }
                if operation == "workspace.refresh" {
                    let approved = approved_pages
                        .lock()
                        .map_err(|_| "operation_in_progress".to_owned())?;
                    let access = managed_notes_from_refresh(&value, &approved)
                        .map_err(|_| "malformed_response".to_owned())?;
                    *managed_notes
                        .lock()
                        .map_err(|_| "operation_in_progress".to_owned())? = Some(access);
                }
                Ok(value)
            }
            Err(error) => {
                let code = error.code();
                if code == "session_exhausted"
                    || !matches!(error, BridgeError::Server(_) | BridgeError::InvalidRequest)
                {
                    guard.take();
                }
                Err(code)
            }
        }
    })
    .await
    .map_err(|_| "runtime_unavailable".to_owned())?
}

#[tauri::command]
pub(crate) async fn desktop_reveal_managed_note(
    state: State<'_, DesktopState>,
    vault_path: String,
    relative_path: String,
) -> Result<(), String> {
    let managed_notes = Arc::clone(&state.managed_notes);
    tauri::async_runtime::spawn_blocking(move || {
        let selected = {
            let guard = managed_notes
                .lock()
                .map_err(|_| "operation_in_progress".to_owned())?;
            let access = guard
                .as_ref()
                .ok_or_else(|| "note_not_approved".to_owned())?;
            reveal_path(access, &vault_path, &relative_path)?
        };
        crate::bridge::reveal_file(&selected).map_err(|error| error.code())
    })
    .await
    .map_err(|_| "runtime_unavailable".to_owned())?
}

#[tauri::command]
pub(crate) async fn desktop_reconnect(state: State<'_, DesktopState>) -> Result<(), String> {
    let session = Arc::clone(&state.session);
    let approved_pages = Arc::clone(&state.approved_pages);
    let managed_notes = Arc::clone(&state.managed_notes);
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = session
            .try_lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
        *approved_pages
            .lock()
            .map_err(|_| "operation_in_progress".to_owned())? = HashSet::new();
        *managed_notes
            .lock()
            .map_err(|_| "operation_in_progress".to_owned())? = None;
        if let Some(mut bridge) = guard.take()
            && !bridge.shutdown()
        {
            return Err("cleanup_unconfirmed".to_owned());
        }
        Ok(())
    })
    .await
    .map_err(|_| "runtime_unavailable".to_owned())?
}

fn connect(app: &AppHandle, data_dir: Option<&Path>) -> Result<Bridge, String> {
    if !matches!(
        (std::env::consts::OS, std::env::consts::ARCH),
        ("macos", "aarch64") | ("linux", "x86_64")
    ) {
        return Err("unsupported_platform".to_owned());
    }
    let resolve = |relative: &str| {
        app.path()
            .resolve(relative, BaseDirectory::Resource)
            .map_err(|_| "runtime_unavailable".to_owned())
    };
    let core = resolve("binaries/runtime/bin/open-brain")?;
    validate_runtime_pair(
        &core,
        &resolve("binaries/runtime/libexec/open-brain-graphify")?,
        &resolve("binaries/desktop-component-manifest-v1.json")?,
    )
    .map_err(|error| error.code())?;
    let mut bridge = Bridge::spawn_selected(&core, data_dir).map_err(|error| error.code())?;
    let handshake = bridge
        .invoke("system.handshake", json!({}), None, Duration::from_secs(15))
        .map_err(|error| error.code())?;
    validate_handshake(&handshake)?;
    Ok(bridge)
}

pub(crate) fn validate_handshake(value: &Value) -> Result<(), String> {
    let operations = value.get("operations").and_then(Value::as_array);
    if value.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
        || value.get("protocol_version").and_then(Value::as_u64) != Some(PROTOCOL_VERSION)
        || value.get("runtime_session_version").and_then(Value::as_u64)
            != Some(SUPPORTED_RUNTIME_SESSION)
        || value.get("state_schema_version").and_then(Value::as_u64) != Some(SUPPORTED_STATE_SCHEMA)
        || value.get("product_version").and_then(Value::as_str) != Some("0.1.0")
        || !value
            .get("brain_root")
            .and_then(Value::as_str)
            .is_some_and(|root| Path::new(root).is_absolute())
        || !BASE_OPERATIONS.iter().all(|operation| {
            operations
                .is_some_and(|items| items.iter().any(|item| item.as_str() == Some(operation)))
        })
    {
        return Err("incompatible_runtime".to_owned());
    }
    Ok(())
}

fn managed_notes_from_refresh(
    value: &Value,
    approved_pages: &HashSet<String>,
) -> Result<ManagedNotes, ()> {
    let wire_vault = PathBuf::from(value.get("vault_path").and_then(Value::as_str).ok_or(())?);
    if !wire_vault.is_absolute()
        || fs::symlink_metadata(&wire_vault)
            .map_err(|_| ())?
            .file_type()
            .is_symlink()
    {
        return Err(());
    }
    let canonical_vault = fs::canonicalize(&wire_vault).map_err(|_| ())?;
    if !canonical_vault.is_dir() {
        return Err(());
    }
    let notes = value.get("notes").and_then(Value::as_array).ok_or(())?;
    let mut relative_paths = HashSet::new();
    for note in notes {
        let note_id = note.get("note_id").and_then(Value::as_str).ok_or(())?;
        if !approved_pages.contains(note_id) {
            continue;
        }
        let relative = PathBuf::from(
            note.get("relative_path")
                .and_then(Value::as_str)
                .ok_or(())?,
        );
        validate_relative_note(&canonical_vault, &relative)?;
        relative_paths.insert(relative);
    }
    Ok(ManagedNotes {
        wire_vault,
        canonical_vault,
        relative_paths,
    })
}

fn validate_relative_note(vault: &Path, relative: &Path) -> Result<PathBuf, ()> {
    if relative.as_os_str().is_empty()
        || relative.is_absolute()
        || relative
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(());
    }
    let mut candidate = vault.to_path_buf();
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return Err(());
        };
        candidate.push(name);
        if fs::symlink_metadata(&candidate)
            .map_err(|_| ())?
            .file_type()
            .is_symlink()
        {
            return Err(());
        }
    }
    let canonical = fs::canonicalize(&candidate).map_err(|_| ())?;
    if !canonical.starts_with(vault) || !canonical.is_file() {
        return Err(());
    }
    Ok(canonical)
}

fn reveal_path(
    access: &ManagedNotes,
    vault_path: &str,
    relative_path: &str,
) -> Result<PathBuf, String> {
    let wire_vault = Path::new(vault_path);
    let relative = Path::new(relative_path);
    if wire_vault != access.wire_vault || !access.relative_paths.contains(relative) {
        return Err("note_not_approved".to_owned());
    }
    let current_vault = fs::canonicalize(wire_vault).map_err(|_| "unsafe_vault_path".to_owned())?;
    if current_vault != access.canonical_vault {
        return Err("unsafe_vault_path".to_owned());
    }
    validate_relative_note(&current_vault, relative).map_err(|_| "unsafe_vault_path".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn handshake() -> Value {
        json!({
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "product_version": "0.1.0",
            "runtime_session_version": 2,
            "state_schema_version": 7,
            "brain_root": "/synthetic/brain",
            "operations": BASE_OPERATIONS,
        })
    }

    #[test]
    fn same_version_without_runtime_floor_cannot_open_the_brain() {
        assert!(validate_handshake(&handshake()).is_ok());
        for field in [
            "runtime_session_version",
            "state_schema_version",
            "operations",
        ] {
            let mut old = handshake();
            old.as_object_mut().unwrap().remove(field);
            assert_eq!(
                validate_handshake(&old),
                Err("incompatible_runtime".to_owned())
            );
        }
    }

    #[test]
    fn unknown_schema_and_relative_brain_are_rejected() {
        for unsupported in [4, 5, 6, 8] {
            let mut wrong_version = handshake();
            wrong_version["state_schema_version"] = json!(unsupported);
            assert!(validate_handshake(&wrong_version).is_err());
        }
        let mut relative = handshake();
        relative["brain_root"] = json!("brain");
        assert!(validate_handshake(&relative).is_err());
    }
    #[test]
    fn t03_desktop_admits_only_the_frozen_new_runtime() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/new-user-t03/compatibility.json"
        ))
        .unwrap();
        let mut next = handshake();
        next["state_schema_version"] = fixture["versions"]["proposed"]["storage"].clone();
        next["runtime_session_version"] =
            fixture["versions"]["proposed"]["runtime_session"].clone();
        assert!(validate_handshake(&next).is_ok());
        let raw = serde_json::to_string(&next).unwrap().replace(
            "\"runtime_session_version\":2",
            "\"runtime_session_version\":2.0",
        );
        let fractional = crate::strict_json::from_slice(raw.as_bytes()).unwrap();
        assert!(validate_handshake(&fractional).is_err());
        for version in ["baseline", "current"] {
            let mut old = next.clone();
            old["state_schema_version"] = fixture["versions"][version]["storage"].clone();
            old["runtime_session_version"] =
                fixture["versions"][version]["runtime_session"].clone();
            assert_eq!(
                validate_handshake(&old),
                Err("incompatible_runtime".to_owned())
            );
        }
    }

    #[test]
    fn reveal_scope_accepts_only_refreshed_regular_notes_without_symlinks() {
        let directory = tempfile::TempDir::new().unwrap();
        let vault = directory.path().join("vault");
        fs::create_dir(&vault).unwrap();
        fs::write(vault.join("Approved.md"), "synthetic").unwrap();
        let refresh = json!({
            "vault_path": vault,
            "notes": [{"note_id": "page_synthetic", "revision_id": "revision_synthetic", "relative_path": "Approved.md"}],
        });
        let access =
            managed_notes_from_refresh(&refresh, &HashSet::from(["page_synthetic".to_owned()]))
                .unwrap();
        assert_eq!(
            reveal_path(&access, vault.to_str().unwrap(), "Approved.md").unwrap(),
            fs::canonicalize(vault.join("Approved.md")).unwrap()
        );
        assert_eq!(
            reveal_path(&access, vault.to_str().unwrap(), "../outside.md"),
            Err("note_not_approved".to_owned())
        );
        assert_eq!(
            reveal_path(&access, vault.to_str().unwrap(), "Other.md"),
            Err("note_not_approved".to_owned())
        );
        std::os::unix::fs::symlink(directory.path().join("outside.md"), vault.join("Linked.md"))
            .unwrap();
        let unsafe_refresh = json!({
            "vault_path": vault,
            "notes": [{"note_id": "page_other", "revision_id": "revision_other", "relative_path": "Linked.md"}],
        });
        assert!(
            managed_notes_from_refresh(&unsafe_refresh, &HashSet::from(["page_other".to_owned()]))
                .is_err()
        );
    }
}
