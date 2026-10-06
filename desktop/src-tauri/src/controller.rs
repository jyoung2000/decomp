//! Controller sidecar lifecycle.
//!
//! Contract with the Python controller (see docs/API.md):
//!   `rebuild-controller serve` (packaged) / `python -m rebuild_controller.cli.main serve` (dev)
//!   binds a loopback port and atomically writes `<data_dir>/controller.json`
//!   (`{"port": u16, "token": str, "pid": u32}`). The shell removes any stale file before
//!   spawning, waits until the file parses AND `GET /health` answers on that port, injects
//!   `window.__REBUILD_STUDIO__ = {baseUrl, token}` into the main window, and kills the whole
//!   process tree on exit.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::ShellExt;

use crate::paths;
use crate::procs::{configure_for_tree, kill_tree, now_ms, open_log, tail_lines};

pub const SIDECAR_NAME: &str = "rebuild-controller";
pub const EVENT_EXITED: &str = "rebuild://controller-exited";
const DEFAULT_STARTUP_TIMEOUT_SECS: u64 = 90;
/// A real PyInstaller/standalone sidecar is megabytes; the dev stub is a few bytes.
const MIN_REAL_SIDECAR_BYTES: u64 = 64 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ControllerFile {
    pub port: u16,
    pub token: String,
    #[serde(default)]
    pub pid: u32,
}

impl ControllerFile {
    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Mode {
    Sidecar,
    DevPython,
    External,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExitInfo {
    pub code: Option<i32>,
    pub at_ms: u64,
}

#[derive(Debug)]
pub enum StartError {
    SidecarMissing(String),
    Spawn(String),
    ExitedEarly { code: Option<i32>, log_path: PathBuf, tail: String },
    Timeout { secs: u64, log_path: PathBuf, tail: String },
}

impl StartError {
    pub fn log_path(&self) -> Option<&Path> {
        match self {
            StartError::ExitedEarly { log_path, .. } | StartError::Timeout { log_path, .. } => Some(log_path),
            _ => None,
        }
    }
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::SidecarMissing(m) => write!(f, "The analysis controller is missing: {m}"),
            StartError::Spawn(m) => write!(f, "The analysis controller could not be started: {m}"),
            StartError::ExitedEarly { code, tail, .. } => {
                write!(f, "The analysis controller exited during startup (exit code {}).", code.map(|c| c.to_string()).unwrap_or_else(|| "unknown".into()))?;
                if !tail.is_empty() {
                    write!(f, "\n\nLast log lines:\n{tail}")?;
                }
                Ok(())
            }
            StartError::Timeout { secs, tail, .. } => {
                write!(f, "The analysis controller did not become ready within {secs} seconds.")?;
                if !tail.is_empty() {
                    write!(f, "\n\nLast log lines:\n{tail}")?;
                }
                Ok(())
            }
        }
    }
}

#[derive(Default)]
struct Inner {
    child: Option<Child>,
    mode: Option<Mode>,
    started_at_ms: Option<u64>,
    exited: Option<ExitInfo>,
    last_error: Option<String>,
}

#[derive(Default)]
pub struct ControllerState {
    inner: Mutex<Inner>,
    shutting_down: AtomicBool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    pub mode: Option<Mode>,
    pub process_alive: bool,
    pub started_at_ms: Option<u64>,
    pub exited: Option<ExitInfo>,
    pub last_error: Option<String>,
}

impl ControllerState {
    pub fn snapshot(&self) -> Snapshot {
        let mut inner = self.inner.lock().unwrap();
        let alive = match inner.child.as_mut() {
            Some(c) => matches!(c.try_wait(), Ok(None)),
            None => inner.mode == Some(Mode::External),
        };
        Snapshot {
            mode: inner.mode,
            process_alive: alive,
            started_at_ms: inner.started_at_ms,
            exited: inner.exited.clone(),
            last_error: inner.last_error.clone(),
        }
    }

    pub fn record_error(&self, message: String) {
        self.inner.lock().unwrap().last_error = Some(message);
    }

    /// Kill the controller tree (idempotent) and drop the now-stale controller.json.
    pub fn shutdown(&self, data_dir: &Path) {
        self.shutting_down.store(true, Ordering::SeqCst);
        let child = {
            let mut inner = self.inner.lock().unwrap();
            inner.child.take()
        };
        if let Some(mut child) = child {
            kill_tree(&mut child, Duration::from_secs(5));
            let _ = std::fs::remove_file(paths::controller_json(data_dir));
        }
    }
}

// ---------------------------------------------------------------------------
// controller.json + readiness
// ---------------------------------------------------------------------------

/// `Ok(None)` when absent or not yet a complete JSON document (the controller writes atomically,
/// but be tolerant of a half-written file on exotic filesystems).
pub fn read_controller_file(path: &Path) -> Option<ControllerFile> {
    let bytes = std::fs::read(path).ok()?;
    let f: ControllerFile = serde_json::from_slice(&bytes).ok()?;
    (f.port != 0 && !f.token.is_empty()).then_some(f)
}

/// True if something on 127.0.0.1:port speaks HTTP and answers `/health`.
pub fn probe_health(port: u16, token: &str) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = s.set_write_timeout(Some(Duration::from_millis(1500)));
    let req = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
    );
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 16];
    matches!(s.read(&mut buf), Ok(n) if n >= 7 && buf.starts_with(b"HTTP/1."))
}

pub enum WaitFailure {
    ExitedEarly(Option<i32>),
    Timeout,
}

/// Poll until controller.json parses and `/health` answers.
/// `exit_check` returns `Some(code)` when the process is known to be gone (fail fast).
pub fn wait_ready(
    json_path: &Path,
    exit_check: &mut dyn FnMut() -> Option<Option<i32>>,
    timeout: Duration,
    poll: Duration,
) -> Result<ControllerFile, WaitFailure> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(code) = exit_check() {
            return Err(WaitFailure::ExitedEarly(code));
        }
        if let Some(f) = read_controller_file(json_path) {
            if probe_health(f.port, &f.token) {
                return Ok(f);
            }
        }
        if Instant::now() >= deadline {
            return Err(WaitFailure::Timeout);
        }
        std::thread::sleep(poll);
    }
}

// ---------------------------------------------------------------------------
// Launch
// ---------------------------------------------------------------------------

fn env_flag(name: &str) -> bool {
    matches!(std::env::var(name).ok().as_deref(), Some("1") | Some("true") | Some("yes"))
}

fn startup_timeout() -> Duration {
    let secs = std::env::var("REBUILD_STUDIO_STARTUP_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_STARTUP_TIMEOUT_SECS);
    Duration::from_secs(secs)
}

fn sidecar_is_real(program: &std::ffi::OsStr) -> bool {
    std::fs::metadata(program)
        .map(|m| m.is_file() && m.len() >= MIN_REAL_SIDECAR_BYTES)
        .unwrap_or(false)
}

fn python_fallback() -> Command {
    let program = std::env::var_os("REBUILD_STUDIO_PYTHON")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| if cfg!(windows) { "python".into() } else { "python3".into() });
    let mut cmd = Command::new(program);
    cmd.args(["-m", "rebuild_controller.cli.main", "serve"]);
    if let Some(dir) = std::env::var_os("REBUILD_STUDIO_CONTROLLER_DIR").filter(|v| !v.is_empty()) {
        // Dev convenience: run from a source checkout without installing the package.
        cmd.env("PYTHONPATH", &dir).current_dir(&dir);
    }
    cmd
}

fn plan(app: &AppHandle) -> Result<(Command, Mode), StartError> {
    let force_python = env_flag("REBUILD_STUDIO_DEV_PYTHON");
    if !force_python {
        match app.shell().sidecar(SIDECAR_NAME) {
            Ok(shell_cmd) => {
                let mut cmd: Command = shell_cmd.into();
                if sidecar_is_real(cmd.get_program()) {
                    cmd.arg("serve");
                    return Ok((cmd, Mode::Sidecar));
                }
            }
            Err(e) => eprintln!("rebuild-studio: sidecar lookup failed: {e}"),
        }
    }
    if cfg!(debug_assertions) || force_python || env_flag("REBUILD_STUDIO_ALLOW_PYTHON_FALLBACK") {
        Ok((python_fallback(), Mode::DevPython))
    } else {
        Err(StartError::SidecarMissing(format!(
            "'{SIDECAR_NAME}' was not found next to the application. Reinstall Rebuild Studio, or run Doctor-RebuildStudio.ps1."
        )))
    }
}

/// Start (or attach to) the controller and wait for it to be ready.
pub fn start(app: &AppHandle, state: &ControllerState, data_dir: &Path) -> Result<ControllerFile, StartError> {
    let json_path = paths::controller_json(data_dir);
    let log_path = paths::controller_log(data_dir);
    let timeout = startup_timeout();
    std::fs::create_dir_all(data_dir).map_err(|e| StartError::Spawn(format!("cannot create data dir {}: {e}", data_dir.display())))?;

    if env_flag("REBUILD_STUDIO_CONTROLLER_EXTERNAL") {
        // Attach to an already running controller (dev). Never killed by the shell.
        return match wait_ready(&json_path, &mut || None, timeout, Duration::from_millis(200)) {
            Ok(f) => {
                let mut inner = state.inner.lock().unwrap();
                inner.mode = Some(Mode::External);
                inner.started_at_ms = Some(now_ms());
                Ok(f)
            }
            Err(_) => Err(StartError::Timeout { secs: timeout.as_secs(), log_path, tail: String::new() }),
        };
    }

    let (mut cmd, mode) = plan(app)?;
    let _ = std::fs::remove_file(&json_path); // never trust a stale token/port
    let log = open_log(&log_path, 5 * 1024 * 1024).map_err(|e| StartError::Spawn(format!("cannot open {}: {e}", log_path.display())))?;
    let log_err = log.try_clone().map_err(|e| StartError::Spawn(e.to_string()))?;
    {
        use std::io::Write as _;
        let mut l = &log;
        let _ = writeln!(l, "--- shell starting controller ({mode:?}) at unix_ms={} ---", now_ms());
    }
    cmd.env(paths::ENV_DATA, data_dir)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    if std::env::var_os(paths::ENV_TOOLS).is_none() {
        cmd.env(paths::ENV_TOOLS, paths::tools_dir());
    }
    if std::env::var_os("REBUILD_STUDIO_INSTALL").is_none() {
        if let Some(dir) = tauri::utils::platform::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf)) {
            cmd.env("REBUILD_STUDIO_INSTALL", dir);
        }
    }
    configure_for_tree(&mut cmd);

    let child = cmd
        .spawn()
        .map_err(|e| StartError::Spawn(format!("{}: {e}", cmd.get_program().to_string_lossy())))?;
    #[cfg(windows)]
    {
        if !crate::procs::job::assign(&child) {
            eprintln!("rebuild-studio: could not assign controller to the kill-on-close job object");
        }
    }
    // Register the child immediately so a shell shutdown during startup still kills it.
    {
        let mut inner = state.inner.lock().unwrap();
        inner.child = Some(child);
        inner.mode = Some(mode);
        inner.exited = None;
        inner.last_error = None;
    }

    let mut exit_check = || -> Option<Option<i32>> {
        if state.shutting_down.load(Ordering::SeqCst) {
            return Some(None);
        }
        let mut inner = state.inner.lock().unwrap();
        match inner.child.as_mut() {
            None => Some(None),
            Some(c) => match c.try_wait() {
                Ok(Some(status)) => Some(status.code()),
                _ => None,
            },
        }
    };
    match wait_ready(&json_path, &mut exit_check, timeout, Duration::from_millis(150)) {
        Ok(file) => {
            state.inner.lock().unwrap().started_at_ms = Some(now_ms());
            Ok(file)
        }
        Err(failure) => {
            let child = state.inner.lock().unwrap().child.take();
            if let Some(mut c) = child {
                kill_tree(&mut c, Duration::from_secs(2));
            }
            let tail = tail_lines(&log_path, 25);
            Err(match failure {
                WaitFailure::ExitedEarly(code) => StartError::ExitedEarly { code, log_path, tail },
                WaitFailure::Timeout => StartError::Timeout { secs: timeout.as_secs(), log_path, tail },
            })
        }
    }
}

/// Background watcher: tells the UI if the controller dies after it was ready.
pub fn spawn_monitor(app: AppHandle) {
    std::thread::Builder::new()
        .name("controller-monitor".into())
        .spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            let app_state = app.state::<crate::AppState>();
            let state = &app_state.controller;
            if state.shutting_down.load(Ordering::SeqCst) {
                return;
            }
            let exited = {
                let mut inner = state.inner.lock().unwrap();
                match inner.child.as_mut().map(|c| c.try_wait()) {
                    Some(Ok(Some(status))) => {
                        let info = ExitInfo { code: status.code(), at_ms: now_ms() };
                        inner.child = None;
                        inner.exited = Some(info.clone());
                        Some(info)
                    }
                    Some(Ok(None)) => None,
                    Some(Err(_)) | None => return,
                }
            };
            if let Some(info) = exited {
                let _ = app.emit(EVENT_EXITED, info);
                return;
            }
        })
        .ok();
}

/// JS injected before any page script runs in the main window.
pub fn init_script(file: &ControllerFile) -> String {
    let payload = serde_json::json!({
        "baseUrl": file.base_url(),
        "token": file.token,
        "shell": "tauri",
        "shellVersion": env!("CARGO_PKG_VERSION"),
    });
    // JSON is a JS expression; escape the two characters that are valid in JSON but not in older JS parsers.
    let json = payload.to_string().replace('\u{2028}', "\\u2028").replace('\u{2029}', "\\u2029");
    format!(
        "(function(){{try{{Object.defineProperty(window,'__REBUILD_STUDIO__',{{value:Object.freeze({json}),writable:false,configurable:false,enumerable:true}});}}catch(e){{console.error('rebuild-studio: cannot inject controller info',e);}}}})();"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("rs-ctl-{tag}-{}", now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn serve_health_once(listener: TcpListener) {
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { return };
                let mut buf = [0u8; 512];
                let _ = s.read(&mut buf);
                let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
            }
        });
    }

    #[test]
    fn parses_controller_json_and_ignores_garbage() {
        let d = tmpdir("parse");
        let p = d.join("controller.json");
        assert!(read_controller_file(&p).is_none());
        std::fs::write(&p, b"{\"port\": 41").unwrap();
        assert!(read_controller_file(&p).is_none());
        std::fs::write(&p, br#"{"port":0,"token":"t"}"#).unwrap();
        assert!(read_controller_file(&p).is_none());
        std::fs::write(&p, br#"{"port":4711,"token":"abc","pid":99,"extra":true}"#).unwrap();
        let f = read_controller_file(&p).unwrap();
        assert_eq!(f, ControllerFile { port: 4711, token: "abc".into(), pid: 99 });
        assert_eq!(f.base_url(), "http://127.0.0.1:4711");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn wait_ready_succeeds_when_file_appears_and_server_answers() {
        let d = tmpdir("ready");
        let json = d.join("controller.json");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        serve_health_once(listener);
        let json2 = json.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            std::fs::write(&json2, format!(r#"{{"port":{port},"token":"tok","pid":1}}"#)).unwrap();
        });
        let f = wait_ready(&json, &mut || None, Duration::from_secs(10), Duration::from_millis(50)).ok().unwrap();
        assert_eq!(f.port, port);
        assert_eq!(f.token, "tok");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn wait_ready_times_out_when_nothing_listens() {
        let d = tmpdir("timeout");
        let json = d.join("controller.json");
        // Reserve then free a port so nothing answers on it.
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        std::fs::write(&json, format!(r#"{{"port":{port},"token":"tok"}}"#)).unwrap();
        let r = wait_ready(&json, &mut || None, Duration::from_millis(400), Duration::from_millis(50));
        assert!(matches!(r, Err(WaitFailure::Timeout)));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[cfg(unix)]
    #[test]
    fn wait_ready_fails_fast_when_child_exits() {
        let d = tmpdir("early");
        let json = d.join("controller.json");
        let mut child = Command::new("false").spawn().unwrap();
        let t0 = Instant::now();
        let mut check = || child.try_wait().ok().flatten().map(|s| s.code());
        let r = wait_ready(&json, &mut check, Duration::from_secs(30), Duration::from_millis(50));
        assert!(matches!(r, Err(WaitFailure::ExitedEarly(Some(1)))));
        assert!(t0.elapsed() < Duration::from_secs(5));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn init_script_is_a_frozen_assignment_with_escaped_json() {
        let s = init_script(&ControllerFile { port: 5, token: "a\"b</script>".into(), pid: 1 });
        assert!(s.contains("__REBUILD_STUDIO__"));
        assert!(s.contains("\"baseUrl\":\"http://127.0.0.1:5\""));
        assert!(s.contains("Object.freeze("));
        // The token must appear only as an escaped JSON string.
        assert!(s.contains(r#""token":"a\"b</script>""#));
    }

    #[test]
    fn stub_sidecar_is_not_real() {
        let d = tmpdir("stub");
        let p = d.join("rebuild-controller");
        std::fs::write(&p, b"#!/bin/sh\nexit 3\n").unwrap();
        assert!(!sidecar_is_real(p.as_os_str()));
        std::fs::write(&p, vec![0u8; 128 * 1024]).unwrap();
        assert!(sidecar_is_real(p.as_os_str()));
        assert!(!sidecar_is_real(d.join("missing").as_os_str()));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn start_error_messages_are_readable() {
        let e = StartError::Timeout { secs: 90, log_path: PathBuf::from("x"), tail: "boom".into() };
        let m = e.to_string();
        assert!(m.contains("90 seconds") && m.contains("boom"));
    }
}
