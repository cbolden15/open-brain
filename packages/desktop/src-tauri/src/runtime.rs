use crate::bridge::{Bridge, BridgeError, PROTOCOL, PROTOCOL_VERSION};
use crate::collector::{CollectorConnection, OPERATIONS as SOURCE_OPERATIONS};
use crate::proof::validate_runtime_pair;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager, State, path::BaseDirectory};

const MINIMUM_STATE_SCHEMA: u64 = 6;
const BASE_OPERATIONS: &[&str] = &[
    "system.status",
    "capture.create",
    "search.query",
    "agent.setup.preview",
    "agent.setup.apply",
];
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
    data_dir: Option<PathBuf>,
}

impl DesktopState {
    pub fn new(data_dir: Option<PathBuf>) -> Self {
        Self {
            session: Arc::default(),
            collector: Arc::default(),
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
        || COLLECTOR_OPERATIONS.contains(&operation.as_str())
        || SOURCE_OPERATIONS.contains(&operation.as_str()))
        || !arguments.is_object()
    {
        return Err("invalid_request".to_owned());
    }
    let session = Arc::clone(&state.session);
    let collector = Arc::clone(&state.collector);
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
            Ok(value) => Ok(value),
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
pub(crate) async fn desktop_reconnect(state: State<'_, DesktopState>) -> Result<(), String> {
    let session = Arc::clone(&state.session);
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = session
            .try_lock()
            .map_err(|_| "operation_in_progress".to_owned())?;
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
        || value.get("runtime_session_version").and_then(Value::as_u64) != Some(1)
        || value.get("state_schema_version").and_then(Value::as_u64) != Some(MINIMUM_STATE_SCHEMA)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn handshake() -> Value {
        json!({
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "product_version": "0.1.0",
            "runtime_session_version": 1,
            "state_schema_version": 6,
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
        for unsupported in [4, 5, 7] {
            let mut wrong_version = handshake();
            wrong_version["state_schema_version"] = json!(unsupported);
            assert!(validate_handshake(&wrong_version).is_err());
        }
        let mut relative = handshake();
        relative["brain_root"] = json!("brain");
        assert!(validate_handshake(&relative).is_err());
    }
    #[test]
    fn t03_old_desktop_refuses_new_runtime_before_dispatch() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/new-user-t03/compatibility.json"
        ))
        .unwrap();
        let mut current = handshake();
        current["state_schema_version"] = fixture["versions"]["current"]["storage"].clone();
        assert!(validate_handshake(&current).is_ok());
        let mut next = current.clone();
        next["state_schema_version"] = fixture["versions"]["proposed"]["storage"].clone();
        next["runtime_session_version"] =
            fixture["versions"]["proposed"]["runtime_session"].clone();
        assert_eq!(
            validate_handshake(&next),
            Err("incompatible_runtime".to_owned())
        );
        let raw = serde_json::to_string(&current).unwrap().replace(
            "\"runtime_session_version\":1",
            "\"runtime_session_version\":1.0",
        );
        let fractional = crate::strict_json::from_slice(raw.as_bytes()).unwrap();
        assert!(validate_handshake(&fractional).is_err());
        let mut historical = current.clone();
        historical["state_schema_version"] = fixture["versions"]["baseline"]["storage"].clone();
        assert!(validate_handshake(&historical).is_err());
    }
}
