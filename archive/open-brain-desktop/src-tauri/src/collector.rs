use crate::bridge::{spawn_owned_command, stop_owned_child};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub(crate) const OPERATIONS: &[&str] = &[
    "sources.status",
    "sources.accounts",
    "sources.connect",
    "sources.disconnect",
    "sources.resources",
    "sources.configure",
    "sources.preview",
    "sources.import",
    "sources.control",
    "sources.session_preview",
    "sources.session_apply",
    "sources.background_enable",
    "sources.background_disable",
];

#[derive(Default)]
pub(crate) struct CollectorConnection {
    owned: Option<(Child, i32)>,
}

fn endpoint(brain: &Path) -> Result<(PathBuf, PathBuf), String> {
    let brain = brain
        .canonicalize()
        .map_err(|_| "source_brain_unavailable")?;
    let directory = brain.join("collector");
    if !directory.exists() {
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .map_err(|_| "collector_unavailable")?;
    }
    private_directory(&directory)?;
    let state = directory.join("state.json");
    let identity = format!("{}\0{}", state.display(), brain.display());
    let digest: String = Sha256::digest(identity.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let temp = Path::new("/tmp")
        .canonicalize()
        .map_err(|_| "collector_unavailable")?;
    Ok((
        state,
        temp.join(format!("open-brain-collector-{}", &digest[..24]))
            .join("control.sock"),
    ))
}

fn private_directory(path: &Path) -> Result<(), String> {
    let info = fs::symlink_metadata(path).map_err(|_| "collector_unavailable")?;
    if !info.is_dir() || info.uid() != unsafe { libc::geteuid() } || info.mode() & 0o077 != 0 {
        return Err("collector_unsafe_socket".into());
    }
    Ok(())
}

fn connect_socket(path: &Path) -> Result<UnixStream, String> {
    private_directory(path.parent().ok_or("collector_invalid_path")?)?;
    let info = fs::symlink_metadata(path).map_err(|_| "collector_unavailable")?;
    if !info.file_type().is_socket()
        || info.uid() != unsafe { libc::geteuid() }
        || info.mode() & 0o077 != 0
    {
        return Err("collector_unsafe_socket".into());
    }
    UnixStream::connect(path).map_err(|_| "collector_unavailable".into())
}

fn executable() -> Result<PathBuf, String> {
    let selected = if let Some(path) = std::env::var_os("OPEN_BRAIN_COLLECTOR") {
        PathBuf::from(path)
    } else {
        [
            "/opt/homebrew/bin/open-brain-collector",
            "/usr/local/bin/open-brain-collector",
        ]
        .iter()
        .map(PathBuf::from)
        .find(|path| path.is_file())
        .ok_or("collector_unavailable")?
    };
    if !selected.is_absolute() {
        return Err("collector_unavailable".into());
    }
    let selected = selected
        .canonicalize()
        .map_err(|_| "collector_unavailable")?;
    let info = selected.metadata().map_err(|_| "collector_unavailable")?;
    if !info.is_file() || info.mode() & 0o022 != 0 {
        return Err("collector_unavailable".into());
    }
    Ok(selected)
}

fn command(path: &Path) -> Command {
    let mut command = Command::new(path);
    command.env_clear();
    for name in [
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
    ] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);
    command
}

impl CollectorConnection {
    fn stop_owned(&mut self) -> Result<(), String> {
        if let Some((mut child, group)) = self.owned.take()
            && !stop_owned_child(&mut child, group)
        {
            return Err("cleanup_unconfirmed".into());
        }
        Ok(())
    }

    fn ensure(&mut self, brain: &Path, state: &Path, socket: &Path) -> Result<UnixStream, String> {
        match connect_socket(socket) {
            Ok(stream) => return Ok(stream),
            Err(code) if code == "collector_unsafe_socket" => return Err(code),
            Err(_) => {}
        }
        if let Some((child, _)) = &mut self.owned
            && child
                .try_wait()
                .map_err(|_| "collector_unavailable")?
                .is_some()
        {
            self.stop_owned()?;
        }
        if self.owned.is_none() {
            let mut child_command = command(&executable()?);
            child_command
                .arg("run")
                .arg("--state")
                .arg(state)
                .arg("--brain-root")
                .arg(brain)
                .arg("--lease")
                .arg(state.with_file_name("collector.lock"))
                .args(["--control", "--poll-seconds", "1"]);
            self.owned = Some(spawn_owned_command(&mut child_command).map_err(|e| e.code())?);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Ok(stream) = connect_socket(socket) {
                return Ok(stream);
            }
            if Instant::now() >= deadline {
                return Err("collector_unavailable".into());
            }
            thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn request(
        &mut self,
        brain: &Path,
        operation: &str,
        arguments: Value,
    ) -> Result<Value, String> {
        if !OPERATIONS.contains(&operation) || !arguments.is_object() {
            return Err("invalid_request".into());
        }
        let (state, socket) = endpoint(brain)?;
        if operation == "sources.background_enable" || operation == "sources.background_disable" {
            if arguments.as_object().is_none_or(|args| !args.is_empty()) {
                return Err("invalid_request".into());
            }
            self.stop_owned()?;
            let mut installer = command(&executable()?);
            installer
                .args(["sources", "--state"])
                .arg(&state)
                .arg("--brain-root")
                .arg(brain)
                .arg(if operation.ends_with("_enable") {
                    "background-enable"
                } else {
                    "background-disable"
                })
                .stdout(Stdio::piped());
            let (mut child, group) = spawn_owned_command(&mut installer).map_err(|e| e.code())?;
            let deadline = Instant::now() + Duration::from_secs(20);
            while child
                .try_wait()
                .map_err(|_| "collector_service_start_failed")?
                .is_none()
            {
                if Instant::now() >= deadline {
                    stop_owned_child(&mut child, group);
                    return Err("collector_service_start_failed".into());
                }
                thread::sleep(Duration::from_millis(20));
            }
            let mut bytes = Vec::new();
            child
                .stdout
                .take()
                .ok_or("collector_service_start_failed")?
                .take(65_537)
                .read_to_end(&mut bytes)
                .map_err(|_| "collector_service_start_failed")?;
            stop_owned_child(&mut child, group);
            if bytes.len() > 65_536 {
                return Err("collector_invalid_response".into());
            }
            let result: Value =
                serde_json::from_slice(&bytes).map_err(|_| "collector_invalid_response")?;
            if let Some(code) = result.get("failure_code").and_then(Value::as_str) {
                return Err(code.into());
            }
            return Ok(result);
        }
        send(self.ensure(brain, &state, &socket)?, operation, arguments)
    }
}

fn send(mut stream: UnixStream, operation: &str, arguments: Value) -> Result<Value, String> {
    let id = uuid::Uuid::new_v4().simple().to_string();
    let mut payload = serde_json::to_vec(&json!({"schema_version":1,"request_id":id,
        "operation":operation,"arguments":arguments}))
    .map_err(|_| "invalid_request")?;
    if payload.len() >= 65_536 {
        return Err("invalid_request".into());
    }
    payload.push(b'\n');
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|_| "collector_unavailable")?;
    stream
        .write_all(&payload)
        .map_err(|_| "collector_unavailable")?;
    let bytes = receive(&mut stream, Duration::from_secs(240))?;
    if bytes.len() > 262_144 {
        return Err("collector_invalid_response".into());
    }
    let response: Value =
        serde_json::from_slice(&bytes).map_err(|_| "collector_invalid_response")?;
    if response.get("schema_version").and_then(Value::as_u64) != Some(1)
        || response.get("request_id").and_then(Value::as_str) != Some(id.as_str())
    {
        return Err("collector_invalid_response".into());
    }
    if let Some(error) = response.get("error").and_then(Value::as_str) {
        return Err(error.into());
    }
    response
        .get("result")
        .filter(|r| r.is_object())
        .cloned()
        .ok_or("collector_invalid_response".into())
}

fn receive(stream: &mut UnixStream, timeout: Duration) -> Result<Vec<u8>, String> {
    let deadline = Instant::now() + timeout;
    let mut bytes = Vec::new();
    let mut buffer = [0u8; 4096];
    while bytes.len() <= 262_144 {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("collector_lost_response".into());
        }
        stream
            .set_read_timeout(Some(remaining))
            .map_err(|_| "collector_lost_response")?;
        let count = stream
            .read(&mut buffer)
            .map_err(|_| "collector_lost_response")?;
        if count == 0 {
            return Err("collector_lost_response".into());
        }
        bytes.extend_from_slice(&buffer[..count]);
        if buffer[..count].contains(&b'\n') {
            break;
        }
    }
    if bytes.len() > 262_144
        || bytes.last() != Some(&b'\n')
        || bytes[..bytes.len() - 1].contains(&b'\n')
    {
        return Err("collector_invalid_response".into());
    }
    Ok(bytes)
}

impl Drop for CollectorConnection {
    fn drop(&mut self) {
        let _ = self.stop_owned();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::fs::{PermissionsExt, symlink};

    fn responder(response: fn(Value) -> Value) -> UnixStream {
        let (client, mut server) = UnixStream::pair().unwrap();
        thread::spawn(move || {
            let mut line = String::new();
            BufReader::new(&server).read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["operation"], "sources.status");
            let mut data = serde_json::to_vec(&response(request)).unwrap();
            data.push(b'\n');
            server.write_all(&data).unwrap();
        });
        client
    }

    #[test]
    fn control_roundtrip_binds_response_to_request() {
        let client = responder(|request| {
            json!({"schema_version":1,
            "request_id":request["request_id"], "result":{"sources":[]},"error":null})
        });
        assert_eq!(
            send(client, "sources.status", json!({})).unwrap(),
            json!({"sources":[]})
        );
        let client = responder(|_| {
            json!({"schema_version":1,
            "request_id":"other", "result":{}})
        });
        assert_eq!(
            send(client, "sources.status", json!({})).unwrap_err(),
            "collector_invalid_response"
        );
        let client = responder(|request| {
            json!({"schema_version":1,
            "request_id":request["request_id"], "error":"account_not_connected"})
        });
        assert_eq!(
            send(client, "sources.status", json!({})).unwrap_err(),
            "account_not_connected"
        );
    }

    #[test]
    fn rejects_multiple_frames_and_bounds_total_receive_time() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        server.write_all(b"{}\n{}\n").unwrap();
        assert_eq!(
            receive(&mut client, Duration::from_secs(1)).unwrap_err(),
            "collector_invalid_response"
        );
        let (mut client, mut server) = UnixStream::pair().unwrap();
        let producer = thread::spawn(move || {
            for _ in 0..30 {
                if server.write_all(b" ").is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(15));
            }
        });
        let started = Instant::now();
        assert_eq!(
            receive(&mut client, Duration::from_millis(70)).unwrap_err(),
            "collector_lost_response"
        );
        assert!(started.elapsed() < Duration::from_millis(400));
        drop(client);
        producer.join().unwrap();
    }

    #[test]
    fn socket_must_be_private_and_not_a_symlink() {
        let temp = tempfile::tempdir().unwrap();
        fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let socket = temp.path().join("control.sock");
        let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(connect_socket(&socket).is_ok());
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o666)).unwrap();
        assert_eq!(
            connect_socket(&socket).unwrap_err(),
            "collector_unsafe_socket"
        );
        let link = temp.path().join("link.sock");
        symlink(&socket, &link).unwrap();
        assert_eq!(
            connect_socket(&link).unwrap_err(),
            "collector_unsafe_socket"
        );
    }
}
