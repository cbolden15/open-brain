use crate::bridge::{Bridge, BridgeError, PROTOCOL, PROTOCOL_VERSION};
use crate::collector::{CollectorConnection, OPERATIONS as SOURCE_OPERATIONS};
use crate::proof::validate_runtime_pair;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager, State, path::BaseDirectory};
use uuid::Uuid;

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
    reveal: Arc<Mutex<RevealAuthority>>,
    data_dir: Option<PathBuf>,
}

#[derive(Debug)]
struct ManagedNotes {
    session_epoch: u64,
    wire_vault: PathBuf,
    canonical_vault: PathBuf,
    vault_device: u64,
    vault_inode: u64,
    relative_paths: HashSet<PathBuf>,
}

#[derive(Default)]
struct RevealAuthority {
    session_epoch: u64,
    approved_pages: HashSet<String>,
    managed_notes: Option<ManagedNotes>,
}

impl DesktopState {
    pub fn new(data_dir: Option<PathBuf>) -> Self {
        Self {
            session: Arc::default(),
            collector: Arc::default(),
            reveal: Arc::default(),
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
    let reveal = Arc::clone(&state.reveal);
    let data_dir = state.data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || {
        if SOURCE_OPERATIONS.contains(&operation.as_str()) {
            let brain = {
                let mut guard = session
                    .try_lock()
                    .map_err(|_| "operation_in_progress".to_owned())?;
                if guard.is_none() {
                    let bridge = connect(&app, data_dir.as_deref())?;
                    reset_reveal_authority(&reveal)?;
                    *guard = Some(bridge);
                }
                let status = match guard.as_mut().ok_or("bridge_closed")?.invoke(
                    "system.status",
                    json!({}),
                    None,
                    Duration::from_secs(10),
                ) {
                    Ok(status) => status,
                    Err(error) => {
                        let code = error.code();
                        if should_invalidate(&error) {
                            guard.take();
                            reset_reveal_authority(&reveal)?;
                        }
                        return Err(code);
                    }
                };
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
            let bridge = connect(&app, data_dir.as_deref())?;
            reset_reveal_authority(&reveal)?;
            *guard = Some(bridge);
        }
        let bridge = guard.as_mut().ok_or_else(|| "bridge_closed".to_owned())?;
        let response = bridge.invoke(&operation, arguments, request_id, Duration::from_secs(10));
        match response {
            Ok(value) => {
                if matches!(
                    operation.as_str(),
                    "publication.approve" | "publication.edit_and_approve"
                ) {
                    let page_id = match approved_page_from_decision(&value) {
                        Ok(page_id) => page_id,
                        Err(code) => {
                            guard.take();
                            reset_reveal_authority(&reveal)?;
                            return Err(code);
                        }
                    };
                    reveal
                        .lock()
                        .map_err(|_| "operation_in_progress".to_owned())?
                        .approved_pages
                        .insert(page_id);
                }
                if operation == "workspace.refresh" {
                    let mut authority = reveal
                        .lock()
                        .map_err(|_| "operation_in_progress".to_owned())?;
                    let access = match managed_notes_from_refresh(
                        &value,
                        &authority.approved_pages,
                        authority.session_epoch,
                    ) {
                        Ok(access) => access,
                        Err(()) => {
                            drop(authority);
                            guard.take();
                            reset_reveal_authority(&reveal)?;
                            return Err("malformed_response".to_owned());
                        }
                    };
                    authority.managed_notes = Some(access);
                }
                Ok(value)
            }
            Err(error) => {
                let code = error.code();
                if should_invalidate(&error) {
                    guard.take();
                    reset_reveal_authority(&reveal)?;
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
    let session = Arc::clone(&state.session);
    let reveal = Arc::clone(&state.reveal);
    tauri::async_runtime::spawn_blocking(move || {
        let session_guard = session
            .try_lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
        let guard = reveal
            .lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
        let selected =
            authorized_reveal_path(session_guard.is_some(), &guard, &vault_path, &relative_path)?;
        crate::bridge::reveal_file(&selected).map_err(|error| error.code())
    })
    .await
    .map_err(|_| "runtime_unavailable".to_owned())?
}

#[tauri::command]
pub(crate) async fn desktop_reconnect(state: State<'_, DesktopState>) -> Result<(), String> {
    let session = Arc::clone(&state.session);
    let reveal = Arc::clone(&state.reveal);
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = session
            .try_lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
        reset_reveal_authority(&reveal)?;
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

fn should_invalidate(error: &BridgeError) -> bool {
    matches!(error, BridgeError::SessionExhausted)
        || !matches!(error, BridgeError::Server(_) | BridgeError::InvalidRequest)
}

fn reset_reveal_authority(reveal: &Mutex<RevealAuthority>) -> Result<(), String> {
    let mut authority = reveal
        .lock()
        .map_err(|_| "operation_in_progress".to_owned())?;
    authority.session_epoch = authority.session_epoch.wrapping_add(1);
    authority.approved_pages.clear();
    authority.managed_notes = None;
    Ok(())
}

fn exact_keys(value: &Value, expected: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == expected.len() && expected.iter().all(|key| object.contains_key(*key))
    })
}

fn valid_id(value: &Value, prefix: &str) -> bool {
    value
        .as_str()
        .and_then(|text| text.strip_prefix(&format!("{prefix}_")))
        .and_then(|id| Uuid::parse_str(id).ok())
        .is_some()
}

fn approved_page_from_decision(value: &Value) -> Result<String, String> {
    let keys = [
        "status",
        "outcome",
        "decision_id",
        "proposal_id",
        "page_id",
        "publication_id",
        "duplicate",
        "effective_idempotency_key",
    ];
    let status = value.get("status").and_then(Value::as_str);
    if !exact_keys(value, &keys)
        || !matches!(status, Some("approved" | "edited"))
        || value.get("outcome").and_then(Value::as_str) != status
        || !valid_id(&value["decision_id"], "decision")
        || !valid_id(&value["proposal_id"], "proposal")
        || !valid_id(&value["page_id"], "page")
        || !(value["publication_id"].is_null() || valid_id(&value["publication_id"], "publication"))
        || !value["duplicate"].is_boolean()
        || !value["effective_idempotency_key"]
            .as_str()
            .is_some_and(|key| !key.is_empty())
    {
        return Err("malformed_response".to_owned());
    }
    Ok(value["page_id"].as_str().unwrap().to_owned())
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
    session_epoch: u64,
) -> Result<ManagedNotes, ()> {
    let keys = [
        "status",
        "duplicate",
        "generation",
        "note_id",
        "workspace_id",
        "vault_path",
        "notes",
    ];
    if !exact_keys(value, &keys)
        || value.get("status").and_then(Value::as_str) != Some("refreshed")
        || !value["duplicate"].is_boolean()
        || value["generation"].as_u64().is_none()
        || !(value["note_id"].is_null() || valid_id(&value["note_id"], "page"))
        || !valid_id(&value["workspace_id"], "workspace")
    {
        return Err(());
    }
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
    let vault_metadata = fs::metadata(&canonical_vault).map_err(|_| ())?;
    let notes = value.get("notes").and_then(Value::as_array).ok_or(())?;
    let mut relative_paths = HashSet::new();
    for note in notes {
        if !exact_keys(note, &["note_id", "revision_id", "relative_path"])
            || !valid_id(&note["note_id"], "page")
            || !valid_id(&note["revision_id"], "revision")
        {
            return Err(());
        }
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
        session_epoch,
        wire_vault,
        canonical_vault,
        vault_device: vault_metadata.dev(),
        vault_inode: vault_metadata.ino(),
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
    session_epoch: u64,
    vault_path: &str,
    relative_path: &str,
) -> Result<PathBuf, String> {
    let wire_vault = Path::new(vault_path);
    let relative = Path::new(relative_path);
    if access.session_epoch != session_epoch
        || wire_vault != access.wire_vault
        || !access.relative_paths.contains(relative)
    {
        return Err("note_not_approved".to_owned());
    }
    let current_vault = fs::canonicalize(wire_vault).map_err(|_| "unsafe_vault_path".to_owned())?;
    if current_vault != access.canonical_vault {
        return Err("unsafe_vault_path".to_owned());
    }
    let metadata = fs::metadata(&current_vault).map_err(|_| "unsafe_vault_path".to_owned())?;
    if metadata.dev() != access.vault_device || metadata.ino() != access.vault_inode {
        return Err("unsafe_vault_path".to_owned());
    }
    validate_relative_note(&current_vault, relative).map_err(|_| "unsafe_vault_path".to_owned())
}

fn authorized_reveal_path(
    session_active: bool,
    authority: &RevealAuthority,
    vault_path: &str,
    relative_path: &str,
) -> Result<PathBuf, String> {
    if !session_active {
        return Err("note_not_approved".to_owned());
    }
    let access = authority
        .managed_notes
        .as_ref()
        .ok_or_else(|| "note_not_approved".to_owned())?;
    reveal_path(access, authority.session_epoch, vault_path, relative_path)
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
            "status": "refreshed", "duplicate": false, "generation": 1, "note_id": null,
            "workspace_id": "workspace_123e4567-e89b-42d3-a456-426614174700",
            "vault_path": vault,
            "notes": [{"note_id": "page_123e4567-e89b-42d3-a456-426614174400", "revision_id": "revision_123e4567-e89b-42d3-a456-426614174600", "relative_path": "Approved.md"}],
        });
        let access = managed_notes_from_refresh(
            &refresh,
            &HashSet::from(["page_123e4567-e89b-42d3-a456-426614174400".to_owned()]),
            7,
        )
        .unwrap();
        let authority = RevealAuthority {
            session_epoch: 7,
            approved_pages: HashSet::new(),
            managed_notes: Some(access),
        };
        assert_eq!(
            authorized_reveal_path(true, &authority, vault.to_str().unwrap(), "Approved.md")
                .unwrap(),
            fs::canonicalize(vault.join("Approved.md")).unwrap()
        );
        assert_eq!(
            authorized_reveal_path(false, &authority, vault.to_str().unwrap(), "Approved.md"),
            Err("note_not_approved".to_owned())
        );
        let access = authority.managed_notes.as_ref().unwrap();
        assert_eq!(
            reveal_path(&access, 7, vault.to_str().unwrap(), "../outside.md"),
            Err("note_not_approved".to_owned())
        );
        assert_eq!(
            reveal_path(&access, 7, vault.to_str().unwrap(), "Other.md"),
            Err("note_not_approved".to_owned())
        );
        assert_eq!(
            reveal_path(&access, 8, vault.to_str().unwrap(), "Approved.md"),
            Err("note_not_approved".to_owned())
        );
        std::os::unix::fs::symlink(directory.path().join("outside.md"), vault.join("Linked.md"))
            .unwrap();
        let unsafe_refresh = json!({
            "status": "refreshed", "duplicate": false, "generation": 2, "note_id": null,
            "workspace_id": "workspace_123e4567-e89b-42d3-a456-426614174700",
            "vault_path": vault,
            "notes": [{"note_id": "page_123e4567-e89b-42d3-a456-426614174401", "revision_id": "revision_123e4567-e89b-42d3-a456-426614174601", "relative_path": "Linked.md"}],
        });
        assert!(
            managed_notes_from_refresh(
                &unsafe_refresh,
                &HashSet::from(["page_123e4567-e89b-42d3-a456-426614174401".to_owned()]),
                7
            )
            .is_err()
        );

        fs::rename(&vault, directory.path().join("old-vault")).unwrap();
        fs::create_dir(&vault).unwrap();
        fs::write(vault.join("Approved.md"), "replacement").unwrap();
        assert_eq!(
            reveal_path(&access, 7, vault.to_str().unwrap(), "Approved.md"),
            Err("unsafe_vault_path".to_owned())
        );
    }

    #[test]
    fn session_replacement_clears_all_reveal_authority() {
        let authority = Mutex::new(RevealAuthority {
            session_epoch: 3,
            approved_pages: HashSet::from(["page_123e4567-e89b-42d3-a456-426614174400".to_owned()]),
            managed_notes: None,
        });
        reset_reveal_authority(&authority).unwrap();
        let authority = authority.lock().unwrap();
        assert_eq!(authority.session_epoch, 4);
        assert!(authority.approved_pages.is_empty());
        assert!(authority.managed_notes.is_none());
    }
}
