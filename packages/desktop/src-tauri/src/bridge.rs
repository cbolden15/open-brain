use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;
use uuid::Uuid;

pub const PROTOCOL: &str = "open-brain-client";
pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_SESSION_REQUESTS: usize = 2_000;
const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_RESPONSE_BYTES: usize = 5 * 1024 * 1024;
const MAX_GRAPHIFY_BYTES: usize = 16 * 1024;
const GRACEFUL_EXIT: Duration = Duration::from_millis(750);
const TERMINATE_EXIT: Duration = Duration::from_millis(500);
static OWNED_PROCESS_GROUPS: OnceLock<Mutex<OwnedProcessGroups>> = OnceLock::new();

#[derive(Default)]
struct OwnedProcessGroups {
    groups: HashSet<i32>,
    closing: bool,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum BridgeError {
    #[error("bridge_closed")]
    BridgeClosed,
    #[error("deadline_exceeded")]
    DeadlineExceeded,
    #[error("invalid_request")]
    InvalidRequest,
    #[error("lost_response")]
    LostResponse,
    #[error("malformed_response")]
    MalformedResponse,
    #[error("runtime_unavailable")]
    RuntimeUnavailable,
    #[error("session_exhausted")]
    SessionExhausted,
    #[error("transport_failed")]
    TransportFailed,
    #[error("version_mismatch")]
    VersionMismatch,
    #[error("server_error:{0}")]
    Server(String),
}

impl BridgeError {
    pub fn code(&self) -> String {
        match self {
            Self::Server(code) => code.clone(),
            _ => self.to_string(),
        }
    }
}

enum ReaderEvent {
    Line(Vec<u8>),
    Closed,
    Invalid,
}

pub struct Bridge {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    responses: Receiver<ReaderEvent>,
    process_group: i32,
    requests: usize,
    request_limit: usize,
}

pub fn run_graphify_probe(
    executable: &Path,
    temporary_root: &Path,
    deadline: Duration,
) -> Result<String, BridgeError> {
    let executable = exact_executable(executable)?;
    let request = serde_json::to_vec(&json!({
        "notes": [
            {
                "body": "# First\n[[page_123e4567-e89b-42d3-a456-426614174002]]\n",
                "id": "page_123e4567-e89b-42d3-a456-426614174001",
                "path": "first.md"
            },
            {
                "body": "# Second\n",
                "id": "page_123e4567-e89b-42d3-a456-426614174002",
                "path": "second.md"
            }
        ],
        "operation": "extract_markdown",
        "protocol": "open-brain-graphify-helper-v1"
    }))
    .map_err(|_| BridgeError::InvalidRequest)?;
    let mut command = Command::new(executable);
    command
        .arg("--extract-markdown")
        .current_dir(temporary_root)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
    for key in ["HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    let (mut child, process_group) = spawn_owned(&mut command, owned_process_groups())?;
    let mut stdin = child.stdin.take().ok_or(BridgeError::TransportFailed)?;
    let mut stdout = child.stdout.take().ok_or(BridgeError::TransportFailed)?;
    if let Err(error) = set_nonblocking(&stdin) {
        stop_owned_child(&mut child, process_group);
        return Err(error);
    }
    let deadline_at = Instant::now()
        .checked_add(deadline)
        .ok_or(BridgeError::InvalidRequest)?;
    if let Err(error) = write_until(&mut stdin, &request, deadline_at) {
        stop_owned_child(&mut child, process_group);
        return Err(error);
    }
    drop(stdin);
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let mut response = Vec::new();
        let result = stdout
            .by_ref()
            .take((MAX_GRAPHIFY_BYTES + 1) as u64)
            .read_to_end(&mut response)
            .map(|_| response);
        let _ = sender.send(result);
    });
    let remaining = deadline_at.saturating_duration_since(Instant::now());
    let response = match receiver.recv_timeout(remaining) {
        Ok(Ok(response)) if response.len() <= MAX_GRAPHIFY_BYTES => response,
        Ok(_) => {
            stop_owned_child(&mut child, process_group);
            return Err(BridgeError::MalformedResponse);
        }
        Err(_) => {
            stop_owned_child(&mut child, process_group);
            return Err(BridgeError::DeadlineExceeded);
        }
    };
    if !stop_owned_child(&mut child, process_group) {
        return Err(BridgeError::TransportFailed);
    }
    let value: Value =
        crate::strict_json::from_slice(&response).map_err(|_| BridgeError::MalformedResponse)?;
    if value.get("protocol").and_then(Value::as_str) != Some("open-brain-graphify-helper-v1")
        || value.get("status").and_then(Value::as_str) != Some("ok")
        || value.get("pages").and_then(Value::as_array).map(Vec::len) != Some(2)
        || value.get("links").and_then(Value::as_array).map(Vec::len) != Some(1)
    {
        return Err(BridgeError::MalformedResponse);
    }
    Ok("ok".to_owned())
}

impl Bridge {
    pub fn spawn(executable: &Path, brain_root: &Path) -> Result<Self, BridgeError> {
        Self::spawn_with_limit(executable, brain_root, MAX_SESSION_REQUESTS)
    }

    pub fn spawn_selected(
        executable: &Path,
        brain_root: Option<&Path>,
    ) -> Result<Self, BridgeError> {
        Self::spawn_selected_with_limit(executable, brain_root, MAX_SESSION_REQUESTS)
    }

    fn spawn_with_limit(
        executable: &Path,
        brain_root: &Path,
        request_limit: usize,
    ) -> Result<Self, BridgeError> {
        Self::spawn_selected_with_limit(executable, Some(brain_root), request_limit)
    }

    fn spawn_selected_with_limit(
        executable: &Path,
        brain_root: Option<&Path>,
        request_limit: usize,
    ) -> Result<Self, BridgeError> {
        let executable = exact_executable(executable)?;
        if brain_root.is_some_and(|root| !root.is_absolute())
            || request_limit == 0
            || request_limit > MAX_SESSION_REQUESTS
        {
            return Err(BridgeError::InvalidRequest);
        }
        let mut command = Command::new(executable);
        command
            .arg("plugin")
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0);
        if let Some(root) = brain_root {
            command.arg("--data-dir").arg(root);
        }
        for key in [
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "OPEN_BRAIN_COLLECTOR",
            "TMPDIR",
            "WAYLAND_DISPLAY",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
        ] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let (mut child, process_group) = spawn_owned(&mut command, owned_process_groups())?;
        let stdin = child.stdin.take().ok_or(BridgeError::TransportFailed)?;
        let stdout = child.stdout.take().ok_or(BridgeError::TransportFailed)?;
        if let Err(error) = set_nonblocking(&stdin) {
            signal_group(process_group, libc::SIGKILL);
            let _ = child.wait();
            unregister_process_group(process_group);
            return Err(error);
        }
        let (sender, responses) = mpsc::sync_channel(1);
        thread::spawn(move || read_responses(stdout, sender));
        Ok(Self {
            child: Some(child),
            stdin: Some(stdin),
            responses,
            process_group,
            requests: 0,
            request_limit,
        })
    }

    pub fn invoke(
        &mut self,
        operation: &str,
        arguments: Value,
        request_id: Option<String>,
        deadline: Duration,
    ) -> Result<Value, BridgeError> {
        if self.requests >= self.request_limit {
            self.shutdown();
            return Err(BridgeError::SessionExhausted);
        }
        let request_id = request_id.unwrap_or_else(|| format!("plugin_{}", Uuid::new_v4()));
        if !valid_request_id(&request_id) || operation.is_empty() || !arguments.is_object() {
            return Err(BridgeError::InvalidRequest);
        }
        let mut payload = serde_json::to_vec(&json!({
            "arguments": arguments,
            "operation": operation,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
        }))
        .map_err(|_| BridgeError::InvalidRequest)?;
        payload.push(b'\n');
        if payload.len() > MAX_REQUEST_BYTES {
            return Err(BridgeError::InvalidRequest);
        }
        let deadline_at = Instant::now()
            .checked_add(deadline)
            .ok_or(BridgeError::InvalidRequest)?;
        let Some(stdin) = self.stdin.as_mut() else {
            return Err(BridgeError::BridgeClosed);
        };
        match write_until(stdin, &payload, deadline_at) {
            Ok(()) => {}
            Err(BridgeError::DeadlineExceeded) => {
                self.terminate_now();
                return Err(BridgeError::DeadlineExceeded);
            }
            Err(error) => {
                self.terminate_now();
                return Err(error);
            }
        }
        self.requests += 1;
        let remaining = deadline_at.saturating_duration_since(Instant::now());
        let event = match self.responses.recv_timeout(remaining) {
            Ok(event) => event,
            Err(RecvTimeoutError::Timeout) => {
                self.terminate_now();
                return Err(BridgeError::DeadlineExceeded);
            }
            Err(RecvTimeoutError::Disconnected) => {
                self.terminate_now();
                return Err(BridgeError::LostResponse);
            }
        };
        match event {
            ReaderEvent::Line(line) => self.decode_response(&line, &request_id),
            ReaderEvent::Closed => {
                self.terminate_now();
                Err(BridgeError::LostResponse)
            }
            ReaderEvent::Invalid => {
                self.terminate_now();
                Err(BridgeError::MalformedResponse)
            }
        }
    }

    fn decode_response(&mut self, line: &[u8], request_id: &str) -> Result<Value, BridgeError> {
        let response: Value = crate::strict_json::from_slice(line).map_err(|_| {
            self.terminate_now();
            BridgeError::MalformedResponse
        })?;
        let Some(object) = response.as_object() else {
            self.terminate_now();
            return Err(BridgeError::MalformedResponse);
        };
        if object.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
            || object.get("protocol_version").and_then(Value::as_u64) != Some(PROTOCOL_VERSION)
        {
            self.terminate_now();
            return Err(BridgeError::VersionMismatch);
        }
        if object.get("request_id").and_then(Value::as_str) != Some(request_id) {
            self.terminate_now();
            return Err(BridgeError::MalformedResponse);
        }
        match object.get("ok").and_then(Value::as_bool) {
            Some(true) if object.len() == 5 && object.contains_key("result") => {
                Ok(object["result"].clone())
            }
            Some(false) if object.len() == 5 && object.contains_key("error") => {
                let Some(code) = object["error"]
                    .as_object()
                    .filter(|error| error.len() == 1)
                    .and_then(|error| error.get("code"))
                    .and_then(Value::as_str)
                    .filter(|code| !code.is_empty())
                else {
                    self.terminate_now();
                    return Err(BridgeError::MalformedResponse);
                };
                Err(BridgeError::Server(code.to_owned()))
            }
            _ => {
                self.terminate_now();
                Err(BridgeError::MalformedResponse)
            }
        }
    }

    pub fn shutdown(&mut self) -> bool {
        self.stdin.take();
        if self.wait_for_process_group(GRACEFUL_EXIT) {
            return true;
        }
        signal_group(self.process_group, libc::SIGTERM);
        if !self.wait_for_process_group(TERMINATE_EXIT) {
            signal_group(self.process_group, libc::SIGKILL);
            self.wait_for_process_group(TERMINATE_EXIT);
        }
        self.process_group_gone()
    }

    fn terminate_now(&mut self) {
        self.stdin.take();
        signal_group(self.process_group, libc::SIGTERM);
        if !self.wait_for_process_group(TERMINATE_EXIT) {
            signal_group(self.process_group, libc::SIGKILL);
            self.wait_for_process_group(TERMINATE_EXIT);
        }
    }

    fn wait_for_process_group(&mut self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            self.reap_leader();
            if self.process_group_gone() {
                unregister_process_group(self.process_group);
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn reap_leader(&mut self) {
        if self
            .child
            .as_mut()
            .and_then(|child| child.try_wait().ok())
            .flatten()
            .is_some()
        {
            self.child.take();
        }
    }

    fn process_group_gone(&self) -> bool {
        let result = unsafe { libc::kill(-self.process_group, 0) };
        result == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn exact_executable(path: &Path) -> Result<PathBuf, BridgeError> {
    if !path.is_absolute() {
        return Err(BridgeError::RuntimeUnavailable);
    }
    let selected = fs::canonicalize(path).map_err(|_| BridgeError::RuntimeUnavailable)?;
    let metadata = fs::metadata(&selected).map_err(|_| BridgeError::RuntimeUnavailable)?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err(BridgeError::RuntimeUnavailable);
    }
    Ok(selected)
}

fn valid_request_id(value: &str) -> bool {
    value
        .strip_prefix("plugin_")
        .and_then(|id| Uuid::parse_str(id).ok())
        .is_some()
}

fn signal_group(group: i32, signal: i32) {
    unsafe {
        libc::kill(-group, signal);
    }
}

fn read_responses(mut stdout: impl Read, sender: SyncSender<ReaderEvent>) {
    let mut buffer = Vec::new();
    let mut chunk = [0_u8; 8192];
    loop {
        match stdout.read(&mut chunk) {
            Ok(0) => {
                let event = if buffer.is_empty() {
                    ReaderEvent::Closed
                } else {
                    ReaderEvent::Invalid
                };
                let _ = sender.send(event);
                return;
            }
            Ok(count) => {
                buffer.extend_from_slice(&chunk[..count]);
                if buffer.len() > MAX_RESPONSE_BYTES {
                    let _ = sender.send(ReaderEvent::Invalid);
                    return;
                }
                while let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
                    let mut remainder = buffer.split_off(newline + 1);
                    std::mem::swap(&mut buffer, &mut remainder);
                    remainder.truncate(newline);
                    if sender.send(ReaderEvent::Line(remainder)).is_err() {
                        return;
                    }
                }
            }
            Err(_) => {
                let _ = sender.send(ReaderEvent::Closed);
                return;
            }
        }
    }
}

fn set_nonblocking(stdin: &ChildStdin) -> Result<(), BridgeError> {
    let descriptor = stdin.as_raw_fd();
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(descriptor, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        return Err(BridgeError::TransportFailed);
    }
    Ok(())
}

fn write_until(
    stdin: &mut ChildStdin,
    payload: &[u8],
    deadline: Instant,
) -> Result<(), BridgeError> {
    let mut offset = 0;
    while offset < payload.len() {
        if Instant::now() >= deadline {
            return Err(BridgeError::DeadlineExceeded);
        }
        match stdin.write(&payload[offset..]) {
            Ok(0) => return Err(BridgeError::TransportFailed),
            Ok(count) => offset += count,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(BridgeError::DeadlineExceeded);
                }
                thread::sleep(Duration::from_millis(2));
            }
            Err(_) => return Err(BridgeError::TransportFailed),
        }
    }
    Ok(())
}

fn owned_process_groups() -> &'static Mutex<OwnedProcessGroups> {
    OWNED_PROCESS_GROUPS.get_or_init(|| Mutex::new(OwnedProcessGroups::default()))
}

pub(crate) fn spawn_owned_command(command: &mut Command) -> Result<(Child, i32), BridgeError> {
    spawn_owned(command, owned_process_groups())
}

fn spawn_owned(
    command: &mut Command,
    owner: &Mutex<OwnedProcessGroups>,
) -> Result<(Child, i32), BridgeError> {
    let mut owned = owner.lock().map_err(|_| BridgeError::TransportFailed)?;
    if owned.closing {
        return Err(BridgeError::BridgeClosed);
    }
    let mut child = command
        .spawn()
        .map_err(|_| BridgeError::RuntimeUnavailable)?;
    let group = match i32::try_from(child.id()) {
        Ok(group) => group,
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(BridgeError::TransportFailed);
        }
    };
    owned.groups.insert(group);
    Ok((child, group))
}

fn unregister_process_group(group: i32) {
    if let Ok(mut groups) = owned_process_groups().lock() {
        groups.groups.remove(&group);
    }
}

fn process_group_gone(group: i32) -> bool {
    let result = unsafe { libc::kill(-group, 0) };
    result == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}

pub(crate) fn stop_owned_child(child: &mut Child, group: i32) -> bool {
    let graceful_deadline = Instant::now() + GRACEFUL_EXIT;
    while !process_group_gone(group) && Instant::now() < graceful_deadline {
        let _ = child.try_wait();
        thread::sleep(Duration::from_millis(10));
    }
    if !process_group_gone(group) {
        signal_group(group, libc::SIGTERM);
        let terminate_deadline = Instant::now() + TERMINATE_EXIT;
        while !process_group_gone(group) && Instant::now() < terminate_deadline {
            let _ = child.try_wait();
            thread::sleep(Duration::from_millis(10));
        }
    }
    if !process_group_gone(group) {
        signal_group(group, libc::SIGKILL);
        let kill_deadline = Instant::now() + TERMINATE_EXIT;
        while !process_group_gone(group) && Instant::now() < kill_deadline {
            let _ = child.try_wait();
            thread::sleep(Duration::from_millis(10));
        }
    }
    let _ = child.try_wait();
    let stopped = process_group_gone(group);
    if stopped {
        unregister_process_group(group);
    }
    stopped
}

pub(crate) fn reveal_file(path: &Path) -> Result<(), BridgeError> {
    if !path.is_absolute() || !path.is_file() {
        return Err(BridgeError::InvalidRequest);
    }
    let executable = match std::env::consts::OS {
        "macos" => Path::new("/usr/bin/open"),
        "linux" => Path::new("/usr/bin/xdg-open"),
        _ => return Err(BridgeError::InvalidRequest),
    };
    let executable = exact_executable(executable)?;
    let mut command = Command::new(executable);
    command
        .arg(path)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);
    for key in [
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
    ] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    let (mut child, group) = spawn_owned_command(&mut command)?;
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                unregister_process_group(group);
                return if status.success() {
                    Ok(())
                } else {
                    Err(BridgeError::RuntimeUnavailable)
                };
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            _ => {
                stop_owned_child(&mut child, group);
                return Err(BridgeError::DeadlineExceeded);
            }
        }
    }
}

fn begin_shutdown(owner: &Mutex<OwnedProcessGroups>) -> Vec<i32> {
    owner
        .lock()
        .map(|mut owned| {
            owned.closing = true;
            owned.groups.iter().copied().collect()
        })
        .unwrap_or_default()
}

pub fn terminate_all_owned_process_groups() {
    let groups = begin_shutdown(owned_process_groups());
    for group in &groups {
        signal_group(*group, libc::SIGTERM);
    }
    let deadline = Instant::now() + TERMINATE_EXIT;
    while groups.iter().any(|group| !process_group_gone(*group)) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    for group in &groups {
        if !process_group_gone(*group) {
            signal_group(*group, libc::SIGKILL);
        }
    }
    let deadline = Instant::now() + TERMINATE_EXIT;
    while groups.iter().any(|group| !process_group_gone(*group)) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    if let Ok(mut owned) = owned_process_groups().lock() {
        owned.groups.retain(|group| !process_group_gone(*group));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use tempfile::TempDir;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn wait_for_pid(receipt: &Path) -> i32 {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Ok(text) = fs::read_to_string(receipt)
                && let Ok(pid) = text.parse::<i32>()
                && pid > 0
            {
                assert_eq!(unsafe { libc::kill(pid, 0) }, 0);
                return pid;
            }
            assert!(Instant::now() < deadline, "fixture did not become ready");
            thread::sleep(Duration::from_millis(20));
        }
    }

    fn script(directory: &TempDir, body: &str) -> PathBuf {
        let path = directory.path().join(format!("server-{}", Uuid::new_v4()));
        fs::write(&path, format!("#!/usr/bin/python3\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    fn success_server(directory: &TempDir) -> PathBuf {
        script(
            directory,
            r#"import json, os, pathlib, sys
pathlib.Path(__file__).with_suffix(".ready").write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"ok": True, "protocol": "open-brain-client", "protocol_version": 1, "request_id": request["request_id"], "result": {"operation": request["operation"]}}), flush=True)"#,
        )
    }

    #[test]
    fn shutdown_captures_live_children_and_prevents_late_spawns() {
        let directory = TempDir::new().unwrap();
        let executable = script(&directory, "import time; time.sleep(30)");
        let owner = Mutex::new(OwnedProcessGroups::default());
        let mut command = Command::new(executable);
        command.process_group(0);
        let (mut child, group) = spawn_owned(&mut command, &owner).unwrap();
        let shutting_down = begin_shutdown(&owner);
        let refused = matches!(
            spawn_owned(&mut command, &owner),
            Err(BridgeError::BridgeClosed)
        );
        signal_group(group, libc::SIGKILL);
        child.wait().unwrap();
        assert_eq!(shutting_down, vec![group]);
        assert!(refused);
    }

    #[test]
    fn t03_nested_duplicates_close_the_actual_bridge() {
        let directory = TempDir::new().unwrap();
        let executable = script(
            &directory,
            r#"import json, sys, time
request = json.loads(sys.stdin.readline())
raw = json.dumps({"ok": True, "protocol": "open-brain-client", "protocol_version": 1, "request_id": request["request_id"], "result": {"role": 1}})
print(raw.replace('"role": 1', '"role": 1, "role": 2'), flush=True)
time.sleep(30)"#,
        );
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        assert_eq!(
            bridge.invoke("system.handshake", json!({}), None, Duration::from_secs(2)),
            Err(BridgeError::MalformedResponse)
        );
        assert!(bridge.process_group_gone());
    }

    #[test]
    fn malformed_error_envelope_closes_the_bridge() {
        let directory = TempDir::new().unwrap();
        let executable = script(
            &directory,
            r#"import json, sys, time
request = json.loads(sys.stdin.readline())
print(json.dumps({"ok": False, "protocol": "open-brain-client", "protocol_version": 1, "request_id": request["request_id"], "error": {"code": 1}}), flush=True)
time.sleep(30)"#,
        );
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        assert_eq!(
            bridge.invoke("system.handshake", json!({}), None, Duration::from_secs(2)),
            Err(BridgeError::MalformedResponse)
        );
        assert_eq!(
            bridge.invoke("system.handshake", json!({}), None, Duration::from_secs(2)),
            Err(BridgeError::BridgeClosed)
        );
        assert!(bridge.process_group_gone());
    }

    #[test]
    fn frames_requests_and_enforces_session_exhaustion() {
        let directory = TempDir::new().unwrap();
        let executable = success_server(&directory);
        let mut bridge = Bridge::spawn_with_limit(&executable, directory.path(), 1).unwrap();
        wait_for_pid(&executable.with_extension("ready"));
        let result = bridge
            .invoke("system.handshake", json!({}), None, Duration::from_secs(3))
            .unwrap();
        assert_eq!(result["operation"], "system.handshake");
        assert_eq!(
            bridge.invoke("search.query", json!({}), None, Duration::from_secs(1)),
            Err(BridgeError::SessionExhausted)
        );
    }

    #[test]
    fn spawn_forwards_only_the_optional_collector_executable_path() {
        let _guard = ENV_LOCK.lock().unwrap();
        let directory = TempDir::new().unwrap();
        let executable = script(
            &directory,
            r#"import json, os, pathlib, sys
pathlib.Path(sys.argv[-1]).write_text(os.environ.get("OPEN_BRAIN_COLLECTOR", ""))
request = json.loads(sys.stdin.readline())
print(json.dumps({"ok": True, "protocol": "open-brain-client", "protocol_version": 1, "request_id": request["request_id"], "result": {}}), flush=True)"#,
        );
        let collector = directory.path().join("open-brain-collector");
        fs::write(&collector, "#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&collector, fs::Permissions::from_mode(0o700)).unwrap();
        let receipt = directory.path().join("collector-env");
        let old_collector = std::env::var_os("OPEN_BRAIN_COLLECTOR");
        let old_secret = std::env::var_os("OPEN_BRAIN_COLLECTOR_SECRET");
        unsafe {
            std::env::set_var("OPEN_BRAIN_COLLECTOR", &collector);
            std::env::set_var("OPEN_BRAIN_COLLECTOR_SECRET", "must-not-forward");
        }
        let mut bridge = Bridge::spawn(&executable, &receipt).unwrap();
        bridge
            .invoke("system.handshake", json!({}), None, Duration::from_secs(3))
            .unwrap();
        unsafe {
            match old_collector {
                Some(value) => std::env::set_var("OPEN_BRAIN_COLLECTOR", value),
                None => std::env::remove_var("OPEN_BRAIN_COLLECTOR"),
            }
            match old_secret {
                Some(value) => std::env::set_var("OPEN_BRAIN_COLLECTOR_SECRET", value),
                None => std::env::remove_var("OPEN_BRAIN_COLLECTOR_SECRET"),
            }
        }

        assert_eq!(
            fs::read_to_string(receipt).unwrap(),
            collector.to_string_lossy()
        );
    }

    #[test]
    fn rejects_deadline_malformed_lost_and_version_mismatch() {
        let directory = TempDir::new().unwrap();
        let cases = [
            ("import time; time.sleep(30)", BridgeError::DeadlineExceeded),
            (
                "print('not-json', flush=True)",
                BridgeError::MalformedResponse,
            ),
            ("pass", BridgeError::LostResponse),
            (
                r#"import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"ok": True, "protocol": "open-brain-client", "protocol_version": 2, "request_id": request["request_id"], "result": {}}), flush=True)"#,
                BridgeError::VersionMismatch,
            ),
        ];
        for (body, expected) in cases {
            let executable = script(&directory, body);
            let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
            let deadline = if expected == BridgeError::DeadlineExceeded {
                Duration::from_millis(75)
            } else {
                Duration::from_secs(3)
            };
            assert_eq!(
                bridge.invoke("system.handshake", json!({}), None, deadline),
                Err(expected)
            );
        }
    }

    #[test]
    fn lost_response_keeps_the_supplied_operation_id() {
        let directory = TempDir::new().unwrap();
        let receipt = directory.path().join("request-id");
        let body = format!(
            "import json, pathlib, sys\nrequest = json.loads(sys.stdin.readline())\npathlib.Path({receipt:?}).write_text(request['request_id'])"
        );
        let executable = script(&directory, &body);
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        let request_id = "plugin_123e4567-e89b-42d3-a456-426614174000";
        assert_eq!(
            bridge.invoke(
                "capture.create",
                json!({"text": "synthetic"}),
                Some(request_id.to_owned()),
                Duration::from_secs(3),
            ),
            Err(BridgeError::LostResponse)
        );
        assert_eq!(fs::read_to_string(receipt).unwrap(), request_id);
    }

    #[test]
    fn deadline_terminates_the_owned_process_group() {
        let directory = TempDir::new().unwrap();
        let receipt = directory.path().join("grandchild-pid");
        let body = format!(
            "import pathlib, subprocess, time\nchild = subprocess.Popen(['/bin/sleep', '30'])\npathlib.Path({receipt:?}).write_text(str(child.pid))\ntime.sleep(30)"
        );
        let executable = script(&directory, &body);
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        let pid = wait_for_pid(&receipt);
        assert_eq!(
            bridge.invoke(
                "system.handshake",
                json!({}),
                None,
                Duration::from_millis(100),
            ),
            Err(BridgeError::DeadlineExceeded)
        );
        let deadline = Instant::now() + Duration::from_secs(2);
        while unsafe { libc::kill(pid, 0) } == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    }

    #[test]
    fn deadline_bounds_a_child_that_never_reads_the_request() {
        let directory = TempDir::new().unwrap();
        let executable = script(&directory, "import time; time.sleep(30)");
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        let oversized_pipe_payload = "x".repeat(60 * 1024);

        assert_eq!(
            bridge.invoke(
                "capture.create",
                json!({"text": oversized_pipe_payload}),
                None,
                Duration::from_millis(75),
            ),
            Err(BridgeError::DeadlineExceeded)
        );
    }

    #[test]
    fn oversized_response_closes_and_cleans_the_owned_bridge() {
        let directory = TempDir::new().unwrap();
        let executable = script(
            &directory,
            r#"import sys
sys.stdin.readline()
sys.stdout.write('x' * (5 * 1024 * 1024 + 1))
sys.stdout.flush()"#,
        );
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        assert_eq!(
            bridge.invoke("system.handshake", json!({}), None, Duration::from_secs(3)),
            Err(BridgeError::MalformedResponse)
        );
        assert!(bridge.process_group_gone());
    }

    #[test]
    fn graceful_leader_exit_still_cleans_up_its_descendant() {
        let directory = TempDir::new().unwrap();
        let receipt = directory.path().join("descendant-pid");
        let descendant_body = format!(
            "import os, pathlib, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\npathlib.Path({receipt:?}).write_text(str(os.getpid()))\ntime.sleep(30)"
        );
        let body = format!(
            "import json, subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {descendant_body:?}])\nrequest = json.loads(sys.stdin.readline())\nprint(json.dumps({{'ok': True, 'protocol': 'open-brain-client', 'protocol_version': 1, 'request_id': request['request_id'], 'result': {{}}}}), flush=True)\nsys.stdin.read()"
        );
        let executable = script(&directory, &body);
        let mut bridge = Bridge::spawn(&executable, directory.path()).unwrap();
        let descendant = wait_for_pid(&receipt);
        bridge
            .invoke("system.handshake", json!({}), None, Duration::from_secs(2))
            .unwrap();
        assert!(bridge.shutdown());
        assert_eq!(unsafe { libc::kill(descendant, 0) }, -1);
    }

    #[test]
    fn graphify_probe_executes_one_bounded_structural_operation() {
        let directory = TempDir::new().unwrap();
        let executable = script(
            &directory,
            r#"import json, sys
request = json.loads(sys.stdin.read())
assert request["protocol"] == "open-brain-graphify-helper-v1"
print(json.dumps({"diagnostics": [], "links": [{"source": request["notes"][0]["id"], "target": request["notes"][1]["id"], "kind": "explicit_reference"}], "pages": [note["id"] for note in request["notes"]], "protocol": request["protocol"], "status": "ok"}))"#,
        );

        assert_eq!(
            run_graphify_probe(&executable, directory.path(), Duration::from_secs(2)).unwrap(),
            "ok"
        );
    }
}
