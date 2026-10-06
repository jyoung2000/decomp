//! Tauri commands exposed to the webview. Every command here must also be listed in
//! `build.rs` (app manifest) and granted in `capabilities/default.json`.

use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::{AppHandle, Manager, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::controller::{read_controller_file, ExitInfo, Mode};
use crate::credentials;
use crate::error::{CmdError, CmdResult};
use crate::paths;
use crate::procs::{LaunchSpec, PreviewInfo};
use crate::AppState;

// ----------------------------------------------------------------------- pick_folder

#[tauri::command]
pub async fn pick_folder(
    app: AppHandle,
    window: WebviewWindow,
    title: Option<String>,
    default_path: Option<String>,
) -> CmdResult<Option<String>> {
    let picked = tauri::async_runtime::spawn_blocking(move || {
        let mut b = app.dialog().file().set_parent(&window).set_can_create_directories(true);
        b = b.set_title(title.unwrap_or_else(|| "Select a folder".to_string()));
        if let Some(dir) = default_path.as_deref().filter(|d| Path::new(d).is_dir()) {
            b = b.set_directory(dir);
        }
        b.blocking_pick_folder()
    })
    .await
    .map_err(|e| CmdError::new("dialog_failed", e.to_string()))?;
    match picked {
        None => Ok(None),
        Some(fp) => {
            let p = fp
                .into_path()
                .map_err(|e| CmdError::new("dialog_failed", e.to_string()))?;
            Ok(Some(paths::strip_verbatim(&p).to_string_lossy().into_owned()))
        }
    }
}

// ----------------------------------------------------------------------- open_path / open_url

/// Extensions that would *execute* something when "opened" by the shell. The UI may reveal
/// them in the file manager, but never open them.
const EXECUTABLE_EXTENSIONS: &[&str] = &[
    "exe", "com", "bat", "cmd", "ps1", "psm1", "psd1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "msi",
    "msp", "scr", "lnk", "hta", "cpl", "reg", "jar", "dll", "sh", "bash", "app", "appimage", "pif",
    "url", "gadget", "msc", "inf",
];

pub fn is_executable_extension(path: &Path) -> bool {
    path.extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase())
        .map(|e| EXECUTABLE_EXTENSIONS.contains(&e.as_str()))
        .unwrap_or(false)
}

/// Pure policy check for `open_path`; returns the path to hand to the OS.
pub fn check_open_target(raw: &str, reveal: bool) -> CmdResult<PathBuf> {
    if raw.is_empty() || raw.contains('\0') || raw.len() > 32 * 1024 {
        return Err(CmdError::invalid("path must be a non-empty string"));
    }
    let p = PathBuf::from(raw);
    if !p.is_absolute() {
        return Err(CmdError::invalid("path must be absolute"));
    }
    if !p.exists() {
        return Err(CmdError::not_found(format!("path does not exist: {raw}")));
    }
    if !reveal && p.is_file() && is_executable_extension(&p) {
        return Err(CmdError::new(
            "open_denied",
            "executable files cannot be opened from the UI; use reveal instead",
        ));
    }
    Ok(p)
}

#[tauri::command]
pub fn open_path(app: AppHandle, path: String, reveal: Option<bool>) -> CmdResult<()> {
    let reveal = reveal.unwrap_or(false);
    let p = check_open_target(&path, reveal)?;
    let opener = app.opener();
    if reveal {
        opener
            .reveal_item_in_dir(&p)
            .map_err(|e| CmdError::new("open_failed", e.to_string()))
    } else {
        opener
            .open_path(p.to_string_lossy().into_owned(), None::<&str>)
            .map_err(|e| CmdError::new("open_failed", e.to_string()))
    }
}

pub fn check_open_url(url: &str) -> CmdResult<()> {
    let lower = url.to_ascii_lowercase();
    let ok = (lower.starts_with("http://") || lower.starts_with("https://"))
        && url.len() <= 2048
        && !url.chars().any(|c| c.is_control() || c == ' ');
    if ok {
        Ok(())
    } else {
        Err(CmdError::invalid("only http(s) URLs without whitespace can be opened"))
    }
}

#[tauri::command]
pub fn open_url(app: AppHandle, url: String) -> CmdResult<()> {
    check_open_url(&url)?;
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| CmdError::new("open_failed", e.to_string()))
}

// ----------------------------------------------------------------------- previews

#[tauri::command]
pub fn launch_preview(state: State<'_, AppState>, spec: LaunchSpec) -> CmdResult<PreviewInfo> {
    state.previews.launch(&state.data_dir, spec)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StopResult {
    pub id: String,
    pub was_running: bool,
}

#[tauri::command]
pub fn stop_preview(state: State<'_, AppState>, id: String) -> CmdResult<StopResult> {
    let was_running = state.previews.stop(&id)?;
    Ok(StopResult { id, was_running })
}

#[tauri::command]
pub fn preview_list(state: State<'_, AppState>) -> Vec<PreviewInfo> {
    state.previews.list()
}

// ----------------------------------------------------------------------- credentials

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialSetResult {
    pub backend: &'static str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialGetResult {
    pub found: bool,
    pub secret: Option<String>,
    pub backend: &'static str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialDeleteResult {
    pub deleted: bool,
    pub backend: &'static str,
}

#[tauri::command]
pub fn credential_set(state: State<'_, AppState>, name: String, secret: String) -> CmdResult<CredentialSetResult> {
    let backend = credentials::set(&state.data_dir, &name, &secret)?;
    Ok(CredentialSetResult { backend })
}

#[tauri::command]
pub fn credential_get(state: State<'_, AppState>, name: String) -> CmdResult<CredentialGetResult> {
    let (secret, backend) = credentials::get(&state.data_dir, &name)?;
    Ok(CredentialGetResult { found: secret.is_some(), secret, backend })
}

#[tauri::command]
pub fn credential_delete(state: State<'_, AppState>, name: String) -> CmdResult<CredentialDeleteResult> {
    let (deleted, backend) = credentials::delete(&state.data_dir, &name)?;
    Ok(CredentialDeleteResult { deleted, backend })
}

// ----------------------------------------------------------------------- controller_info / app_paths

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ControllerInfoResult {
    /// controller.json was readable
    pub available: bool,
    pub base_url: Option<String>,
    pub token: Option<String>,
    pub port: Option<u16>,
    pub pid: Option<u32>,
    pub controller_json: String,
    pub log_path: String,
    pub mode: Option<Mode>,
    pub process_alive: bool,
    pub started_at_ms: Option<u64>,
    pub exited: Option<ExitInfo>,
    pub last_error: Option<String>,
}

#[tauri::command]
pub fn controller_info(state: State<'_, AppState>) -> ControllerInfoResult {
    let json_path = paths::controller_json(&state.data_dir);
    let file = read_controller_file(&json_path);
    let snap = state.controller.snapshot();
    ControllerInfoResult {
        available: file.is_some(),
        base_url: file.as_ref().map(|f| f.base_url()),
        token: file.as_ref().map(|f| f.token.clone()),
        port: file.as_ref().map(|f| f.port),
        pid: file.as_ref().map(|f| f.pid),
        controller_json: json_path.to_string_lossy().into_owned(),
        log_path: paths::controller_log(&state.data_dir).to_string_lossy().into_owned(),
        mode: snap.mode,
        process_alive: snap.process_alive,
        started_at_ms: snap.started_at_ms,
        exited: snap.exited,
        last_error: snap.last_error,
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppPaths {
    pub data_dir: String,
    pub tools_dir: String,
    pub logs_dir: String,
    pub credentials_dir: String,
    pub controller_json: String,
    pub app_config_dir: Option<String>,
    pub app_local_data_dir: Option<String>,
    pub resource_dir: Option<String>,
    pub exe_dir: Option<String>,
    pub os: &'static str,
    pub arch: &'static str,
    pub version: &'static str,
    pub credential_backend: &'static str,
}

#[tauri::command]
pub fn app_paths(app: AppHandle, state: State<'_, AppState>) -> AppPaths {
    let s = |p: PathBuf| p.to_string_lossy().into_owned();
    let d = &state.data_dir;
    AppPaths {
        data_dir: s(d.clone()),
        tools_dir: s(paths::tools_dir()),
        logs_dir: s(paths::logs_dir(d)),
        credentials_dir: s(paths::credentials_dir(d)),
        controller_json: s(paths::controller_json(d)),
        app_config_dir: app.path().app_config_dir().ok().map(s),
        app_local_data_dir: app.path().app_local_data_dir().ok().map(s),
        resource_dir: app.path().resource_dir().ok().map(|p| s(paths::strip_verbatim(&p))),
        exe_dir: tauri::utils::platform::current_exe()
            .ok()
            .and_then(|p| p.parent().map(Path::to_path_buf))
            .map(s),
        os: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        version: env!("CARGO_PKG_VERSION"),
        credential_backend: credentials::backend_name(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_policy_rejects_relative_missing_and_executables() {
        assert_eq!(check_open_target("", false).unwrap_err().code, "invalid_argument");
        assert_eq!(check_open_target("relative/dir", false).unwrap_err().code, "invalid_argument");
        let tmp = std::env::temp_dir().join(format!("rs-open-{}", crate::procs::now_ms()));
        std::fs::create_dir_all(&tmp).unwrap();
        assert_eq!(
            check_open_target(tmp.join("nope.txt").to_str().unwrap(), false).unwrap_err().code,
            "not_found"
        );
        let exe = tmp.join("Game.EXE");
        std::fs::write(&exe, b"x").unwrap();
        assert_eq!(check_open_target(exe.to_str().unwrap(), false).unwrap_err().code, "open_denied");
        assert!(check_open_target(exe.to_str().unwrap(), true).is_ok(), "reveal of an exe is allowed");
        let txt = tmp.join("report.html");
        std::fs::write(&txt, b"x").unwrap();
        assert!(check_open_target(txt.to_str().unwrap(), false).is_ok());
        assert!(check_open_target(tmp.to_str().unwrap(), false).is_ok());
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn url_policy_is_http_only() {
        assert!(check_open_url("http://127.0.0.1:5173/").is_ok());
        assert!(check_open_url("https://example.com/a?b=c").is_ok());
        for bad in ["file:///etc/passwd", "javascript:alert(1)", "ms-msdt:/id", "http://a b", "", "HTTP://x\n"] {
            assert!(check_open_url(bad).is_err(), "{bad}");
        }
    }
}
