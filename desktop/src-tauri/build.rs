// Declares the app command ACL so only the commands listed here can be invoked
// from the webview (and only from windows named in capabilities/default.json).
fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
            "pick_folder",
            "open_path",
            "open_url",
            "launch_preview",
            "stop_preview",
            "preview_list",
            "credential_set",
            "credential_get",
            "credential_delete",
            "controller_info",
            "app_paths",
        ])),
    )
    .expect("failed to run tauri-build");
}
