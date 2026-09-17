use crate::bridge::{Bridge, BridgeError, PROTOCOL, PROTOCOL_VERSION, run_graphify_probe};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::time::Duration;
use tauri::{AppHandle, Manager, path::BaseDirectory};
use tempfile::Builder;
use thiserror::Error;
use uuid::Uuid;

const DESKTOP_VERSION: &str = "0.1.0";
const EXPECTED_CORE_VERSION: &str = "0.1.0";
const TARGET: &str = env!("OPEN_BRAIN_DESKTOP_TARGET");

#[derive(Debug, Deserialize)]
struct ComponentManifest {
    components: Vec<Component>,
    core_version: String,
    desktop_version: String,
    protocol: String,
    protocol_version: u64,
    schema_version: u64,
    target: String,
}

#[derive(Debug, Deserialize)]
struct Component {
    file: String,
    role: String,
    sha256: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeProofResult {
    capture_status: String,
    cleanup_confirmed: bool,
    core_sha256: String,
    graphify_status: String,
    graphify_sha256: String,
    matched_search_results: usize,
    product_version: String,
    protocol: String,
    protocol_version: u64,
    target: String,
}

#[derive(Debug, Error)]
pub enum ProofError {
    #[error("bridge:{0}")]
    Bridge(#[from] BridgeError),
    #[error("component_manifest_invalid")]
    ComponentManifestInvalid,
    #[error("native_proof_failed")]
    NativeProofFailed,
    #[error("runtime_digest_mismatch")]
    RuntimeDigestMismatch,
    #[error("runtime_unavailable")]
    RuntimeUnavailable,
}

impl ProofError {
    pub fn code(&self) -> String {
        match self {
            Self::Bridge(error) => error.code(),
            _ => self.to_string(),
        }
    }
}

#[tauri::command]
pub async fn run_native_proof(app: AppHandle) -> Result<NativeProofResult, String> {
    tauri::async_runtime::spawn_blocking(move || run(&app))
        .await
        .map_err(|_| "native_proof_failed".to_owned())?
        .map_err(|error| error.code())
}

fn run(app: &AppHandle) -> Result<NativeProofResult, ProofError> {
    let core = app
        .path()
        .resolve("binaries/runtime/bin/open-brain", BaseDirectory::Resource)
        .map_err(|_| ProofError::RuntimeUnavailable)?;
    let graphify = app
        .path()
        .resolve(
            "binaries/runtime/libexec/open-brain-graphify",
            BaseDirectory::Resource,
        )
        .map_err(|_| ProofError::RuntimeUnavailable)?;
    let manifest_path = app
        .path()
        .resolve(
            "binaries/desktop-component-manifest-v1.json",
            BaseDirectory::Resource,
        )
        .map_err(|_| ProofError::ComponentManifestInvalid)?;
    run_with_paths(&core, &graphify, &manifest_path)
}

pub fn run_with_paths(
    core: &Path,
    graphify: &Path,
    manifest_path: &Path,
) -> Result<NativeProofResult, ProofError> {
    let manifest: ComponentManifest = serde_json::from_slice(
        &fs::read(manifest_path).map_err(|_| ProofError::ComponentManifestInvalid)?,
    )
    .map_err(|_| ProofError::ComponentManifestInvalid)?;
    validate_manifest(&manifest)?;
    let core_sha256 = verify_component(core, &manifest, "core")?;
    let graphify_sha256 = verify_component(graphify, &manifest, "graphify")?;
    let synthetic = Builder::new()
        .prefix("open-brain-desktop-d0-")
        .tempdir()
        .map_err(|_| ProofError::RuntimeUnavailable)?;
    fs::set_permissions(synthetic.path(), fs::Permissions::from_mode(0o700))
        .map_err(|_| ProofError::RuntimeUnavailable)?;
    let synthetic_root =
        fs::canonicalize(synthetic.path()).map_err(|_| ProofError::RuntimeUnavailable)?;
    let brain_root = synthetic_root.join("brain");
    let mut bridge = Bridge::spawn(core, &brain_root)?;
    let handshake = bridge.invoke("system.handshake", json!({}), None, Duration::from_secs(15))?;
    let product_version = handshake
        .get("product_version")
        .and_then(Value::as_str)
        .filter(|value| *value == manifest.core_version)
        .ok_or(ProofError::NativeProofFailed)?
        .to_owned();
    crate::runtime::validate_handshake(&handshake).map_err(|_| ProofError::NativeProofFailed)?;
    let phrase = format!("synthetic desktop proof {}", Uuid::new_v4());
    let capture = bridge.invoke(
        "capture.create",
        json!({"text": phrase}),
        None,
        Duration::from_secs(5),
    )?;
    let capture_status = capture
        .get("status")
        .and_then(Value::as_str)
        .filter(|status| *status == "captured")
        .ok_or(ProofError::NativeProofFailed)?
        .to_owned();
    let search = bridge.invoke(
        "search.query",
        json!({"limit": 10, "query": phrase}),
        None,
        Duration::from_secs(5),
    )?;
    let matched_search_results = search
        .get("results")
        .and_then(Value::as_array)
        .map(Vec::len)
        .filter(|count| *count == 1)
        .ok_or(ProofError::NativeProofFailed)?;
    let process_group_stopped = bridge.shutdown();
    let graphify_status = run_graphify_probe(graphify, &synthetic_root, Duration::from_secs(15))?;
    let synthetic_path = synthetic.path().to_path_buf();
    synthetic
        .close()
        .map_err(|_| ProofError::NativeProofFailed)?;
    Ok(NativeProofResult {
        capture_status,
        cleanup_confirmed: process_group_stopped && !synthetic_path.exists(),
        core_sha256,
        graphify_status,
        graphify_sha256,
        matched_search_results,
        product_version,
        protocol: PROTOCOL.to_owned(),
        protocol_version: PROTOCOL_VERSION,
        target: TARGET.to_owned(),
    })
}

fn validate_manifest(manifest: &ComponentManifest) -> Result<(), ProofError> {
    if manifest.schema_version != 1
        || manifest.desktop_version != DESKTOP_VERSION
        || manifest.core_version != EXPECTED_CORE_VERSION
        || manifest.protocol != PROTOCOL
        || manifest.protocol_version != PROTOCOL_VERSION
        || manifest.target != TARGET
        || manifest.components.len() != 2
    {
        return Err(ProofError::ComponentManifestInvalid);
    }
    Ok(())
}

pub(crate) fn validate_runtime_pair(
    core: &Path,
    graphify: &Path,
    manifest_path: &Path,
) -> Result<(), ProofError> {
    let manifest: ComponentManifest = serde_json::from_slice(
        &fs::read(manifest_path).map_err(|_| ProofError::ComponentManifestInvalid)?,
    )
    .map_err(|_| ProofError::ComponentManifestInvalid)?;
    validate_manifest(&manifest)?;
    verify_component(core, &manifest, "core")?;
    verify_component(graphify, &manifest, "graphify")?;
    Ok(())
}

fn verify_component(
    executable: &Path,
    manifest: &ComponentManifest,
    role: &str,
) -> Result<String, ProofError> {
    let expected_file = if role == "core" {
        "runtime/bin/open-brain"
    } else {
        "runtime/libexec/open-brain-graphify"
    };
    let component = manifest
        .components
        .iter()
        .find(|component| component.role == role && component.file == expected_file)
        .ok_or(ProofError::ComponentManifestInvalid)?;
    let metadata = fs::metadata(executable).map_err(|_| ProofError::RuntimeUnavailable)?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err(ProofError::RuntimeUnavailable);
    }
    let digest = sha256(executable)?;
    if component.sha256 != digest {
        return Err(ProofError::RuntimeDigestMismatch);
    }
    Ok(digest)
}

fn sha256(path: &Path) -> Result<String, ProofError> {
    let mut file = fs::File::open(path).map_err(|_| ProofError::RuntimeUnavailable)?;
    let mut hasher = Sha256::new();
    let mut chunk = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut chunk)
            .map_err(|_| ProofError::RuntimeUnavailable)?;
        if count == 0 {
            break;
        }
        hasher.update(&chunk[..count]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn compatible_manifest() -> ComponentManifest {
        ComponentManifest {
            components: vec![
                Component {
                    file: "runtime/bin/open-brain".to_owned(),
                    role: "core".to_owned(),
                    sha256: String::new(),
                },
                Component {
                    file: "runtime/libexec/open-brain-graphify".to_owned(),
                    role: "graphify".to_owned(),
                    sha256: String::new(),
                },
            ],
            core_version: EXPECTED_CORE_VERSION.to_owned(),
            desktop_version: DESKTOP_VERSION.to_owned(),
            protocol: PROTOCOL.to_owned(),
            protocol_version: PROTOCOL_VERSION,
            schema_version: 1,
            target: TARGET.to_owned(),
        }
    }

    #[test]
    fn manifest_rejects_a_protocol_mismatch() {
        let mut manifest = compatible_manifest();
        assert!(validate_manifest(&manifest).is_ok());
        manifest.protocol_version += 1;
        assert!(matches!(
            validate_manifest(&manifest),
            Err(ProofError::ComponentManifestInvalid)
        ));
    }

    #[test]
    fn changed_runtime_is_rejected_before_launch() {
        let directory = tempfile::tempdir().unwrap();
        let executable = directory.path().join("open-brain");
        fs::write(&executable, b"synthetic original runtime").unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let mut manifest = compatible_manifest();
        manifest.components[0].sha256 = sha256(&executable).unwrap();
        assert!(verify_component(&executable, &manifest, "core").is_ok());
        fs::write(&executable, b"synthetic replaced runtime").unwrap();
        assert!(matches!(
            verify_component(&executable, &manifest, "core"),
            Err(ProofError::RuntimeDigestMismatch)
        ));
    }
}
