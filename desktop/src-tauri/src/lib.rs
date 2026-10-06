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
            "<p class=\"sub\">Full log: <code>{}</code><br>Run <code>Doctor-RebuildStudio.ps1</code> for a dependency check. Close this window to exit.</p>",
            html_escape(&lp.to_string_lossy())
        ));
    } else {
        body.push_str("<p class=\"sub\">Run <code>Doctor-RebuildStudio.ps1</code> for a dependency check. Close this window to exit.</p>");
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

fn boot(app: AppHandle) {
    let state = app.state::<AppState>();
    match controller::start(&app, &state.controller, &state.data_dir) {
        Ok(file) => {
            controller::spawn_monitor(app.clone());
            if let Err(e) = create_main(&app, &file) {
                let msg = format!("The main window could not be created: {e}");
                state.controller.record_error(msg.clone());
                show_error(&app, &msg, None);
            }
        }
        Err(e) => {
            let msg = e.to_string();
            state.controller.record_error(msg.clone());
            let log = e.log_path().map(|p| p.to_path_buf());
            show_error(&app, &msg, log.as_deref());
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

pub fn run() {
    let data_dir = paths::data_dir();

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
    fn error_page_escapes_html_and_is_a_data_url() {
        let u = page("t", "h", "<pre>&lt;script&gt;</pre>");
        assert_eq!(u.scheme(), "data");
        assert!(!u.as_str().contains("<script"));
        assert_eq!(html_escape("<a href=\"x\">&"), "&lt;a href=&quot;x&quot;&gt;&amp;");
    }
}
