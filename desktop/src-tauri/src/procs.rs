//! Child-process helpers: process-tree kill, log redirection, and the preview
//! registry behind `launch_preview` / `stop_preview` / `preview_list`.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
#[cfg(unix)]
use std::time::Instant;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::error::{CmdError, CmdResult};

#[cfg(windows)]
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Make the spawned process the leader of a new process group (posix) so the
/// whole tree can be signalled with `killpg`; hide the console window (Windows).
pub fn configure_for_tree(cmd: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = cmd;
    }
}

/// Open `path` for appending (creating parent dirs); rotate to `<path>.1` above `max_bytes`.
pub fn open_log(path: &Path, max_bytes: u64) -> std::io::Result<File> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    if let Ok(meta) = std::fs::metadata(path) {
        if meta.len() > max_bytes {
            let mut rotated = path.as_os_str().to_owned();
            rotated.push(".1");
            let _ = std::fs::remove_file(&rotated);
            let _ = std::fs::rename(path, &rotated);
        }
    }
    OpenOptions::new().create(true).append(true).open(path)
}

/// Last `max_lines` lines (bounded to the final 64 KiB) of a text file, lossily decoded.
pub fn tail_lines(path: &Path, max_lines: usize) -> String {
    use std::io::{Read, Seek, SeekFrom};
    let Ok(mut f) = File::open(path) else {
        return String::new();
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(64 * 1024);
    let mut buf = Vec::new();
    if f.seek(SeekFrom::Start(start)).is_err() || f.read_to_end(&mut buf).is_err() {
        return String::new();
    }
    let text = String::from_utf8_lossy(&buf);
    let lines: Vec<&str> = text.lines().collect();
    let from = lines.len().saturating_sub(max_lines);
    lines[from..].join("\n")
}

/// Kill `child` and every descendant, then reap it.
///
/// * posix: SIGTERM to the process group (the child must have been spawned through
///   [`configure_for_tree`]), wait up to `grace`, then SIGKILL the group.
/// * Windows: `taskkill /PID <pid> /T /F` (tree kill), falling back to `Child::kill`.
pub fn kill_tree(child: &mut Child, grace: Duration) {
    let pid = child.id();
    #[cfg(unix)]
    {
        let pgid = pid as libc::pid_t;
        // SAFETY: plain signal delivery to a process group we created.
        unsafe {
            libc::killpg(pgid, libc::SIGTERM);
        }
        let deadline = Instant::now() + grace;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
                _ => break,
            }
        }
        // Always SIGKILL the group: the leader may have exited while descendants linger.
        unsafe {
            libc::killpg(pgid, libc::SIGKILL);
        }
        let _ = child.wait();
    }
    #[cfg(windows)]
    {
        let _ = grace;
        let taskkill = std::env::var_os("SystemRoot")
            .map(|r| PathBuf::from(r).join("System32").join("taskkill.exe"))
            .filter(|p| p.exists())
            .unwrap_or_else(|| PathBuf::from("taskkill.exe"));
        let mut cmd = Command::new(taskkill);
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let _ = cmd.status();
        let _ = child.kill();
        let _ = child.wait();
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = (grace, pid);
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// True if `image` (a full executable path) is a packaged controller sidecar.
pub fn is_controller_image(image: &str) -> bool {
    let name = image.rsplit(['\\', '/']).next().unwrap_or("").to_ascii_lowercase();
    name.starts_with("rebuild-controller")
}

/// A controller left behind by a previous shell (e.g. job assignment failed and the shell was killed)
/// still holds the SQLite store. Kill it — tree included — but only after verifying that `pid` really is a
/// controller sidecar executable, never an unrelated process that reused the PID. Returns true if killed.
pub fn reap_orphan_controller(pid: u32) -> bool {
    if pid == 0 || pid == std::process::id() {
        return false;
    }
    #[cfg(windows)]
    {
        let Some(image) = win_image_path(pid) else { return false };
        if !is_controller_image(&image) {
            return false;
        }
        let taskkill = std::env::var_os("SystemRoot")
            .map(|r| PathBuf::from(r).join("System32").join("taskkill.exe"))
            .filter(|p| p.exists())
            .unwrap_or_else(|| PathBuf::from("taskkill.exe"));
        let mut cmd = Command::new(taskkill);
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        return matches!(cmd.status(), Ok(s) if s.success());
    }
    #[cfg(unix)]
    {
        let Ok(exe) = std::fs::read_link(format!("/proc/{pid}/exe")) else { return false };
        if !is_controller_image(&exe.to_string_lossy()) {
            return false;
        }
        // SAFETY: plain signal delivery to a verified process.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
        return true;
    }
    #[allow(unreachable_code)]
    false
}

#[cfg(windows)]
fn win_image_path(pid: u32) -> Option<String> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut len).is_ok();
        let _ = CloseHandle(h);
        ok.then(|| String::from_utf16_lossy(&buf[..len as usize]))
    }
}

// ---------------------------------------------------------------------------
// Windows job object: children assigned to it die when the shell dies, even on a crash.
// ---------------------------------------------------------------------------

#[cfg(windows)]
pub mod job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use std::sync::OnceLock;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    struct JobHandle(HANDLE);
    // SAFETY: a job HANDLE is just a kernel object reference; it is never closed
    // (the OS closes it at process exit, which is exactly what triggers KILL_ON_JOB_CLOSE).
    unsafe impl Send for JobHandle {}
    unsafe impl Sync for JobHandle {}

    static JOB: OnceLock<Option<JobHandle>> = OnceLock::new();

    fn create() -> Option<JobHandle> {
        unsafe {
            let h = CreateJobObjectW(None, PCWSTR::null()).ok()?;
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                h,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
            .ok()?;
            Some(JobHandle(h))
        }
    }

    /// Best-effort: returns false if the job could not be created / the process not assigned.
    pub fn assign(child: &Child) -> bool {
        let Some(job) = JOB.get_or_init(create).as_ref() else {
            return false;
        };
        let proc = HANDLE(child.as_raw_handle());
        unsafe { AssignProcessToJobObject(job.0, proc).is_ok() }
    }
}

// ---------------------------------------------------------------------------
// Preview registry
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LaunchSpec {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PreviewInfo {
    pub id: String,
    pub pid: u32,
    pub command: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub started_at_ms: u64,
    pub running: bool,
    pub exit_code: Option<i32>,
    pub log_path: String,
}

struct Entry {
    info: PreviewInfo,
    child: Child,
}

#[derive(Default)]
pub struct PreviewRegistry {
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    counter: u64,
    entries: Vec<Entry>,
}

const MAX_ARGS: usize = 256;
const MAX_ARG_LEN: usize = 32 * 1024;
const MAX_ENV: usize = 256;
const MAX_TRACKED: usize = 64;
const DENIED_PROGRAMS: &[&str] = &[
    "cmd", "powershell", "pwsh", "sh", "bash", "zsh", "dash", "fish", "wscript", "cscript", "mshta",
    "rundll32", "regsvr32",
];

/// Defence in depth: previews run built apps, never an interpreter shell handed over by the UI.
pub fn is_denied_program(command: &str) -> bool {
    let name = command
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(command)
        .to_ascii_lowercase();
    let stem = name.strip_suffix(".exe").unwrap_or(&name);
    DENIED_PROGRAMS.contains(&stem)
}

pub fn validate_spec(spec: &LaunchSpec) -> CmdResult<()> {
    if spec.command.trim().is_empty() || spec.command.contains('\0') {
        return Err(CmdError::invalid("command must be a non-empty string"));
    }
    if is_denied_program(&spec.command) {
        return Err(CmdError::new(
            "command_denied",
            format!("launching '{}' is not allowed from a preview", spec.command),
        ));
    }
    if spec.args.len() > MAX_ARGS || spec.args.iter().any(|a| a.len() > MAX_ARG_LEN || a.contains('\0')) {
        return Err(CmdError::invalid("too many or oversized arguments"));
    }
    if spec.env.len() > MAX_ENV
        || spec
            .env
            .iter()
            .any(|(k, v)| k.is_empty() || k.contains('=') || k.contains('\0') || v.contains('\0'))
    {
        return Err(CmdError::invalid("invalid environment"));
    }
    if let Some(cwd) = &spec.cwd {
        if !Path::new(cwd).is_dir() {
            return Err(CmdError::invalid(format!("cwd is not a directory: {cwd}")));
        }
    }
    Ok(())
}

impl PreviewRegistry {
    pub fn launch(&self, data_dir: &Path, spec: LaunchSpec) -> CmdResult<PreviewInfo> {
        validate_spec(&spec)?;
        let mut inner = self.inner.lock().unwrap();
        Self::reap(&mut inner);
        inner.counter += 1;
        let id = format!("pv-{}-{:x}", inner.counter, now_ms());
        let log_path: PathBuf = crate::paths::previews_log_dir(data_dir).join(format!("{id}.log"));
        let log = open_log(&log_path, 8 * 1024 * 1024).map_err(|e| CmdError::io("open preview log", &e))?;
        let log_err = log.try_clone().map_err(|e| CmdError::io("clone preview log", &e))?;

        let mut cmd = Command::new(&spec.command);
        cmd.args(&spec.args)
            .envs(&spec.env)
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(log_err));
        if let Some(cwd) = &spec.cwd {
            cmd.current_dir(cwd);
        }
        configure_for_tree(&mut cmd);
        let child = cmd
            .spawn()
            .map_err(|e| CmdError::new("spawn_failed", format!("could not start '{}': {e}", spec.command)))?;
        let info = PreviewInfo {
            id,
            pid: child.id(),
            command: spec.command,
            args: spec.args,
            cwd: spec.cwd,
            started_at_ms: now_ms(),
            running: true,
            exit_code: None,
            log_path: log_path.to_string_lossy().into_owned(),
        };
        inner.entries.push(Entry {
            info: info.clone(),
            child,
        });
        // Bound the table: drop the oldest finished entries first.
        while inner.entries.len() > MAX_TRACKED {
            match inner.entries.iter().position(|e| !e.info.running) {
                Some(i) => {
                    inner.entries.remove(i);
                }
                None => break,
            }
        }
        Ok(info)
    }

    fn reap(inner: &mut Inner) {
        for e in inner.entries.iter_mut() {
            if e.info.running {
                if let Ok(Some(status)) = e.child.try_wait() {
                    e.info.running = false;
                    e.info.exit_code = status.code();
                }
            }
        }
    }

    pub fn list(&self) -> Vec<PreviewInfo> {
        let mut inner = self.inner.lock().unwrap();
        Self::reap(&mut inner);
        inner.entries.iter().map(|e| e.info.clone()).collect()
    }

    /// Kill the tree and forget the entry. Returns whether it was still running.
    pub fn stop(&self, id: &str) -> CmdResult<bool> {
        let mut entry = {
            let mut inner = self.inner.lock().unwrap();
            let idx = inner
                .entries
                .iter()
                .position(|e| e.info.id == id)
                .ok_or_else(|| CmdError::not_found(format!("no preview with id '{id}'")))?;
            inner.entries.remove(idx)
        };
        let was_running = matches!(entry.child.try_wait(), Ok(None));
        // Always run the tree kill: the leader may be gone while descendants live on.
        kill_tree(&mut entry.child, Duration::from_secs(3));
        Ok(was_running)
    }

    pub fn kill_all(&self) {
        let entries: Vec<Entry> = std::mem::take(&mut self.inner.lock().unwrap().entries);
        for mut e in entries {
            kill_tree(&mut e.child, Duration::from_secs(2));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orphan_reaper_only_targets_controller_images() {
        assert!(is_controller_image(r"C:\Program Files\Rebuild Studio\rebuild-controller.exe"));
        assert!(is_controller_image("/opt/rs/rebuild-controller-x86_64-unknown-linux-gnu"));
        assert!(!is_controller_image(r"C:\Windows\System32\notepad.exe"));
        assert!(!is_controller_image(r"C:\Python\python.exe"));
        // Our own pid and pid 0 are never reaped; an unrelated live process (this test binary) is not killed.
        assert!(!reap_orphan_controller(0));
        assert!(!reap_orphan_controller(std::process::id()));
    }

    #[cfg(windows)]
    #[test]
    fn orphan_reaper_leaves_unrelated_processes_alone() {
        let mut c = Command::new("cmd.exe").args(["/c", "ping -n 30 127.0.0.1 >NUL"]).spawn().unwrap();
        assert!(!reap_orphan_controller(c.id()));
        assert!(matches!(c.try_wait(), Ok(None)), "unrelated process must survive");
        kill_tree(&mut c, Duration::from_secs(1));
    }

    #[test]
    fn denies_shells_and_script_hosts() {
        for c in ["cmd.exe", "C:\\Windows\\System32\\CMD.EXE", "/bin/sh", "pwsh", "bash", "WScript.exe"] {
            assert!(is_denied_program(c), "{c}");
        }
        for c in ["node", "C:\\x\\game.exe", "./target/debug/app", "python", "shell-app.exe"] {
            assert!(!is_denied_program(c), "{c}");
        }
    }

    #[test]
    fn validate_rejects_bad_specs() {
        let ok = LaunchSpec {
            command: "node".into(),
            args: vec!["a.js".into()],
            cwd: None,
            env: HashMap::new(),
        };
        assert!(validate_spec(&ok).is_ok());
        let mut s = ok.clone();
        s.command = "  ".into();
        assert_eq!(validate_spec(&s).unwrap_err().code, "invalid_argument");
        let mut s = ok.clone();
        s.command = "bash".into();
        assert_eq!(validate_spec(&s).unwrap_err().code, "command_denied");
        let mut s = ok.clone();
        s.cwd = Some("/definitely/not/here".into());
        assert_eq!(validate_spec(&s).unwrap_err().code, "invalid_argument");
        let mut s = ok.clone();
        s.env.insert("A=B".into(), "x".into());
        assert_eq!(validate_spec(&s).unwrap_err().code, "invalid_argument");
    }

    #[cfg(unix)]
    #[test]
    fn launch_list_stop_kills_the_whole_tree() {
        let dir = std::env::temp_dir().join(format!("rs-procs-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let reg = PreviewRegistry::default();
        // The shell is used only inside this test to build a parent with a grandchild;
        // the production path refuses shells.
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "sleep 300 & echo $! > grandchild.pid; wait"])
            .current_dir(&dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        configure_for_tree(&mut cmd);
        let child = cmd.spawn().unwrap();
        let pid = child.id();
        {
            let mut inner = reg.inner.lock().unwrap();
            inner.counter += 1;
            inner.entries.push(Entry {
                info: PreviewInfo {
                    id: "pv-test".into(),
                    pid,
                    command: "sh".into(),
                    args: vec![],
                    cwd: None,
                    started_at_ms: now_ms(),
                    running: true,
                    exit_code: None,
                    log_path: String::new(),
                },
                child,
            });
        }
        let gc_file = dir.join("grandchild.pid");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !gc_file.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        std::thread::sleep(Duration::from_millis(100));
        let gc: i32 = std::fs::read_to_string(&gc_file).unwrap().trim().parse().unwrap();
        assert!(reg.list()[0].running);
        assert_eq!(unsafe { libc::kill(gc, 0) }, 0, "grandchild should be alive");
        assert!(reg.stop("pv-test").unwrap());
        std::thread::sleep(Duration::from_millis(200));
        let alive = unsafe { libc::kill(gc, 0) } == 0;
        // A zombie reparented to init may linger briefly; check via /proc state when present.
        let zombie = std::fs::read_to_string(format!("/proc/{gc}/stat"))
            .map(|s| s.contains(") Z "))
            .unwrap_or(true);
        assert!(!alive || zombie, "grandchild must be dead after stop");
        assert_eq!(reg.stop("pv-test").unwrap_err().code, "not_found");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Gate W10 (docs/WINDOWS_RELEASE_GATES.md): `stop` must end the whole tree on Windows (taskkill /T /F),
    /// including a grandchild that is not a direct child of the launched process. Windows-only.
    #[cfg(windows)]
    #[test]
    fn windows_stop_kills_the_whole_tree() {
        fn alive(pid: u32) -> bool {
            let out = Command::new("tasklist")
                .args(["/FI", &format!("PID eq {pid}"), "/NH"])
                .output()
                .expect("tasklist");
            String::from_utf8_lossy(&out.stdout).contains(&pid.to_string())
        }
        let dir = std::env::temp_dir().join(format!("rs-procs-win-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let reg = PreviewRegistry::default();
        // PowerShell is the parent and ping.exe (300 s) its child; the shell is used only inside this test,
        // the production launch path refuses shells.
        let script = "$p = Start-Process -FilePath ping.exe -ArgumentList '-n','300','127.0.0.1' -WindowStyle Hidden -PassThru; \
                      Set-Content -LiteralPath grandchild.pid -Value $p.Id; Wait-Process -Id $p.Id";
        let mut cmd = Command::new("powershell.exe");
        cmd.args(["-NoProfile", "-NonInteractive", "-Command", script])
            .current_dir(&dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        configure_for_tree(&mut cmd);
        let child = cmd.spawn().unwrap();
        let pid = child.id();
        {
            let mut inner = reg.inner.lock().unwrap();
            inner.counter += 1;
            inner.entries.push(Entry {
                info: PreviewInfo {
                    id: "pv-test".into(),
                    pid,
                    command: "powershell.exe".into(),
                    args: vec![],
                    cwd: None,
                    started_at_ms: now_ms(),
                    running: true,
                    exit_code: None,
                    log_path: String::new(),
                },
                child,
            });
        }
        let gc_file = dir.join("grandchild.pid");
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        while !gc_file.exists() && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(gc_file.exists(), "PowerShell did not start the grandchild within 30 s");
        std::thread::sleep(Duration::from_millis(300));
        let gc: u32 = std::fs::read_to_string(&gc_file)
            .unwrap()
            .trim_matches(|c: char| !c.is_ascii_digit())
            .parse()
            .unwrap();
        assert!(reg.list()[0].running);
        assert!(alive(gc), "grandchild ping.exe should be alive before stop");
        assert!(reg.stop("pv-test").unwrap());
        let gone_by = std::time::Instant::now() + Duration::from_secs(10);
        while alive(gc) && std::time::Instant::now() < gone_by {
            std::thread::sleep(Duration::from_millis(200));
        }
        assert!(!alive(gc), "grandchild must be dead after stop (taskkill /T)");
        assert_eq!(reg.stop("pv-test").unwrap_err().code, "not_found");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn real_launch_records_exit_code_and_log() {
        let dir = std::env::temp_dir().join(format!("rs-launch-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let reg = PreviewRegistry::default();
        let info = reg
            .launch(
                &dir,
                LaunchSpec {
                    command: "printenv".into(),
                    args: vec!["RS_TEST_VAR".into()],
                    cwd: Some(dir.to_string_lossy().into_owned()),
                    env: HashMap::from([("RS_TEST_VAR".to_string(), "hello-preview".to_string())]),
                },
            )
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let l = reg.list();
            if !l[0].running || Instant::now() > deadline {
                assert_eq!(l[0].exit_code, Some(0));
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(std::fs::read_to_string(&info.log_path).unwrap().contains("hello-preview"));
        let _ = reg.stop(&info.id);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
