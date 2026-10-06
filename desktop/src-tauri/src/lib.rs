//! Rebuild Studio desktop shell (Tauri 2).
//!
//! Responsibilities (see docs/DECISIONS.md DR-1/DR-2):
//!  * start the packaged Python controller sidecar, wait for `<data_dir>/controller.json`,
//!    inject `window.__REBUILD_STUDIO__ = {baseUrl, token}` into the UI, kill the sidecar tree on exit;
//!  * native folder picker, open-in-OS, preview process launch/stop (tree kill), credential storage.

mod commands;
mod controller;
mod credentials;
mod error;
mod paths;
mod procs;

use std::path::PathBuf;

use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};

pub struct AppState {
    pub data_dir: PathBuf,
    pub controller: controller::ControllerState,
    pub previews: procs::PreviewRegistry,
}

const MAIN_LABEL: &str = "main";
const STARTUP_LABEL: &str = "startup";
const ERROR_LABEL: &str = "error";

// ---------------------------------------------------------------------------
// Startup / error windows (self-contained data: pages; no IPC, no app assets needed)
// ---------------------------------------------------------------------------

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
}

fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn page(title: &str, heading: &str, body_html: &str) -> Url {
    let html = format!(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{t}</title>\
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">\
<style>:root{{color-scheme:light dark;font:15px/1.5 system-ui,'Segoe UI',sans-serif}}\
body{{margin:0;padding:24px;background:Canvas;color:CanvasText}}\
h1{{font-size:18px;margin:0 0 12px}}pre{{white-space:pre-wrap;word-break:break-word;background:rgba(127,127,127,.15);padding:12px;border-radius:6px;font:12.5px/1.45 ui-monospace,Consolas,monospace}}\
.sub{{opacity:.7}}</style></head><body><h1>{h}</h1>{b}</body></html>",
        t = html_escape(title),
        h = html_escape(heading),
        b = body_html
    );
    Url::parse(&format!("data:text/html;charset=utf-8,{}", percent_encode(&html))).expect("valid data url")
}

fn show_startup(app: &AppHandle) {
    let url = page(
        "Rebuild Studio",
        "Starting Rebuild Studio...",
        "<p class=\"sub\">Starting the local analysis controller. The first start can take a little longer while Windows scans the bundled runtime.</p>",
    );
    let _ = WebviewWindowBuilder::new(app, STARTUP_LABEL, WebviewUrl::External(url))
        .title("Rebuild Studio")
        .inner_size(520.0, 230.0)
        .resizable(false)
        .maximizable(false)
        .center()
        .build();
}

fn show_error(app: &AppHandle, message: &str, log_path: Option<&std::path::Path>) {
    let mut body = format!("<pre>{}</pre>", html_escape(message));
    if let Some(lp) = log_path {
        body.push_str(&format!(
            "<p class=\"sub\">Full log: <code>{}</code><br>Close this window, then start Rebuild Studio again from its icon. If it keeps failing, reinstall it; your projects are kept.</p>",
            html_escape(&lp.to_string_lossy())
        ));
    } else {
        body.push_str("<p class=\"sub\">Close this window, then start Rebuild Studio again from its icon. If it keeps failing, reinstall it; your projects are kept.</p>");
    }
    let url = page("Rebuild Studio - cannot start", "Rebuild Studio could not start", &body);
    let built = WebviewWindowBuilder::new(app, ERROR_LABEL, WebviewUrl::External(url))
        .title("Rebuild Studio - cannot start")
        .inner_size(720.0, 460.0)
        .center()
        .build();
    if built.is_err() {
        eprintln!("rebuild-studio: {message}");
    }
    if let Some(w) = app.get_webview_window(STARTUP_LABEL) {
        let _ = w.close();
    }
}

// ---------------------------------------------------------------------------
// Main window
// ---------------------------------------------------------------------------

/// Only the app's own origin may be navigated to in the main window. In debug builds the
/// Vite dev server on localhost is allowed too.
pub fn allow_navigation(url: &Url, debug: bool) -> bool {
    match url.scheme() {
        "tauri" | "about" => true,
        "http" | "https" => match url.host_str() {
            Some("tauri.localhost") => true,
            Some("localhost") | Some("127.0.0.1") | Some("[::1]") | Some("::1") => debug,
            _ => false,
        },
        _ => false,
    }
}

fn create_main(app: &AppHandle, file: &controller::ControllerFile) -> tauri::Result<()> {
    let cfg = app
        .config()
        .app
        .windows
        .iter()
        .find(|w| w.label == MAIN_LABEL)
        .cloned()
        .expect("tauri.conf.json must declare a window labelled 'main' with create=false");
    let window = WebviewWindowBuilder::from_config(app, &cfg)?
        .initialization_script(controller::init_script(file))
        .on_navigation(|url| {
            let ok = allow_navigation(url, cfg!(debug_assertions));
            if !ok {
                eprintln!("rebuild-studio: blocked navigation to {url}");
            }
            ok
        })
        .build()?;
    let _ = window.set_focus();
    if let Some(w) = app.get_webview_window(STARTUP_LABEL) {
        let _ = w.close();
    }
    Ok(())
}

const RETRY_LABEL: &str = "Try again";
const OPEN_LOG_LABEL: &str = "Open log folder";
const QUIT_LABEL: &str = "Quit";

#[derive(Debug, PartialEq, Eq)]
enum StartupChoice {
    Retry,
    OpenLog,
    Quit,
}

/// Map the native dialog result to an action. Closing the dialog (Cancel) quits.
fn startup_choice(r: &tauri_plugin_dialog::MessageDialogResult) -> StartupChoice {
    use tauri_plugin_dialog::MessageDialogResult as R;
    match r {
        R::Yes => StartupChoice::Retry,
        R::No => StartupChoice::OpenLog,
        R::Custom(s) if s == RETRY_LABEL => StartupChoice::Retry,
        R::Custom(s) if s == OPEN_LOG_LABEL => StartupChoice::OpenLog,
        _ => StartupChoice::Quit,
    }
}

/// Native, blocking recovery dialog for a failed start (no terminal or script needed).
fn ask_startup_recovery(app: &AppHandle, message: &str) -> StartupChoice {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
    if let Some(w) = app.get_webview_window(STARTUP_LABEL) {
        let _ = w.hide();
    }
    let text = format!(
        "{message}\n\nTry again: restarts the analysis controller (an earlier copy is stopped first).\n\
         Open log folder: shows controller.log so you can send it with a bug report.\nQuit: closes Rebuild Studio. Your projects are not affected."
    );
    let r = app
        .dialog()
        .message(text)
        .title("Rebuild Studio could not start")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::YesNoCancelCustom(RETRY_LABEL.into(), OPEN_LOG_LABEL.into(), QUIT_LABEL.into()))
        .blocking_show_with_result();
    startup_choice(&r)
}

fn boot(app: AppHandle) {
    let state = app.state::<AppState>();
    loop {
        let failure = match controller::start(&app, &state.controller, &state.data_dir) {
            Ok(file) => {
                controller::spawn_monitor(app.clone());
                match create_main(&app, &file) {
                    Ok(()) => return,
                    Err(e) => {
                        // Do not leave a headless controller running behind a missing window.
                        state.controller.shutdown(&state.data_dir);
                        state.controller.reset_after_failed_start();
                        format!("The main window could not be created: {e}")
                    }
                }
            }
            Err(e) => match e.log_path() {
                Some(lp) => format!("{e}\n\nLog file: {}", lp.display()),
                None => e.to_string(),
            },
        };
        state.controller.record_error(failure.clone());
        loop {
            match ask_startup_recovery(&app, &failure) {
                StartupChoice::Retry => break,
                StartupChoice::OpenLog => {
                    use tauri_plugin_opener::OpenerExt;
                    if app.opener().open_path(state.data_dir.to_string_lossy(), None::<&str>).is_err() {
                        show_error(&app, &failure, Some(&paths::controller_log(&state.data_dir)));
                        return;
                    }
                }
                StartupChoice::Quit => {
                    app.exit(1);
                    return;
                }
            }
        }
        if let Some(w) = app.get_webview_window(STARTUP_LABEL) {
            let _ = w.show();
        }
    }
}

/// SIGTERM/SIGINT (e.g. Ctrl-C in `cargo tauri dev`, `kill`) must still run the cleanup that kills the
/// controller tree. The handler only flips an atomic (async-signal-safe); a watcher thread turns it
/// into a normal `app.exit(0)`, which fires `RunEvent::Exit`.
#[cfg(unix)]
fn install_signal_exit(app: AppHandle) {
    use std::sync::atomic::{AtomicBool, Ordering};
    static REQUESTED: AtomicBool = AtomicBool::new(false);
    extern "C" fn on_signal(_sig: libc::c_int) {
        REQUESTED.store(true, Ordering::SeqCst);
    }
    // SAFETY: installing a handler that performs only an atomic store.
    unsafe {
        libc::signal(libc::SIGTERM, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
    }
    let _ = std::thread::Builder::new().name("signal-exit".into()).spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(200));
        if REQUESTED.load(Ordering::SeqCst) {
            app.exit(0);
            return;
        }
    });
}

/// Uninstaller entry point (`rebuild-studio.exe --remove-stored-credentials`): runs headless, no window,
/// no controller. Invoked only when the user ticks "Delete the application data" in the uninstaller.
pub const REMOVE_CREDENTIALS_FLAG: &str = "--remove-stored-credentials";

/// Must equal `identifier` in tauri.conf.json: the NSIS installer stamps this AppUserModelID on the Start-menu and
/// desktop shortcuts, and the process sets the same ID so windows group under (and pin as) those shortcuts no matter
/// how the app was started (shortcut, installer "Run", Explorer double-click on the exe).
pub const APP_USER_MODEL_ID: &str = "io.rebuildstudio.desktop";

#[cfg(windows)]
fn set_app_user_model_id() {
    let id: Vec<u16> = APP_USER_MODEL_ID.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: NUL-terminated UTF-16 string that outlives the call; must run before any window is created.
    let _ = unsafe { windows::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(windows::core::PCWSTR(id.as_ptr())) };
}

pub fn run() {
    #[cfg(windows)]
    set_app_user_model_id();
    let data_dir = paths::data_dir();
    if std::env::args().skip(1).any(|a| a == REMOVE_CREDENTIALS_FLAG) {
        let n = credentials::delete_all(&data_dir);
        eprintln!("rebuild-studio: removed {n} stored credential(s)");
        return;
    }

    let mut builder = tauri::Builder::default();
    #[cfg(desktop)]
    {
        // A second instance would delete controller.json and fight over the SQLite store.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window(MAIN_LABEL) {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }));
    }
    builder
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_denylist(&[STARTUP_LABEL, ERROR_LABEL])
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::Builder::new().open_js_links_on_click(false).build())
        .manage(AppState {
            data_dir,
            controller: controller::ControllerState::default(),
            previews: procs::PreviewRegistry::default(),
        })
        .invoke_handler(tauri::generate_handler![
            commands::pick_folder,
            commands::open_path,
            commands::open_url,
            commands::launch_preview,
            commands::stop_preview,
            commands::preview_list,
            commands::credential_set,
            commands::credential_get,
            commands::credential_delete,
            commands::controller_info,
            commands::app_paths,
        ])
        .setup(|app| {
            #[cfg(unix)]
            install_signal_exit(app.handle().clone());
            show_startup(app.handle());
            let handle = app.handle().clone();
            // Windows deadlocks if windows are created from the main thread's setup callback
            // while it blocks; do the slow work (and the window creation) off-thread.
            std::thread::Builder::new()
                .name("boot".into())
                .spawn(move || boot(handle))
                .expect("spawn boot thread");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Rebuild Studio shell")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let state = app.state::<AppState>();
                state.previews.kill_all();
                state.controller.shutdown(&state.data_dir);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn navigation_is_limited_to_the_app_origin() {
        let u = |s: &str| Url::parse(s).unwrap();
        assert!(allow_navigation(&u("tauri://localhost/index.html"), false));
        assert!(allow_navigation(&u("http://tauri.localhost/"), false));
        assert!(allow_navigation(&u("https://tauri.localhost/x"), false));
        assert!(!allow_navigation(&u("https://example.com/"), false));
        assert!(!allow_navigation(&u("http://127.0.0.1:5000/"), false));
        assert!(allow_navigation(&u("http://localhost:5173/"), true));
        assert!(!allow_navigation(&u("file:///etc/passwd"), true));
        assert!(!allow_navigation(&u("javascript:alert(1)"), true));
    }

    #[test]
    fn app_user_model_id_matches_bundle_identifier() {
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(conf["identifier"].as_str(), Some(APP_USER_MODEL_ID));
        assert_eq!(conf["bundle"]["windows"]["nsis"]["installerHooks"].as_str(), Some("windows/hooks.nsh"));
        assert!(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("windows/hooks.nsh").is_file());
    }

    #[test]
    fn startup_dialog_choices_map_to_actions() {
        use tauri_plugin_dialog::MessageDialogResult as R;
        assert_eq!(startup_choice(&R::Yes), StartupChoice::Retry);
        assert_eq!(startup_choice(&R::Custom(RETRY_LABEL.into())), StartupChoice::Retry);
        assert_eq!(startup_choice(&R::No), StartupChoice::OpenLog);
        assert_eq!(startup_choice(&R::Custom(OPEN_LOG_LABEL.into())), StartupChoice::OpenLog);
        assert_eq!(startup_choice(&R::Cancel), StartupChoice::Quit);
        assert_eq!(startup_choice(&R::Custom(QUIT_LABEL.into())), StartupChoice::Quit);
    }

    #[test]
    fn error_page_escapes_html_and_is_a_data_url() {
        let u = page("t", "h", "<pre>&lt;script&gt;</pre>");
        assert_eq!(u.scheme(), "data");
        assert!(!u.as_str().contains("<script"));
        assert_eq!(html_escape("<a href=\"x\">&"), "&lt;a href=&quot;x&quot;&gt;&amp;");
    }
}
