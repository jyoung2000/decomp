//! Data/tools directory resolution. MUST stay identical to
//! `rebuild_controller.config.default_data_dir()` so the shell and the controller
//! agree on where `controller.json`, the SQLite store and the tools live.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

pub const ENV_DATA: &str = "REBUILD_STUDIO_DATA";
pub const ENV_TOOLS: &str = "REBUILD_STUDIO_TOOLS";

/// Pure resolution logic, parameterised for testing.
pub fn data_dir_for(windows: bool, get: &dyn Fn(&str) -> Option<OsString>) -> PathBuf {
    let non_empty = |k: &str| get(k).filter(|v| !v.is_empty());
    if let Some(v) = non_empty(ENV_DATA) {
        return PathBuf::from(v);
    }
    if windows {
        let base = non_empty("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| {
                non_empty("USERPROFILE").map(|h| PathBuf::from(h).join("AppData").join("Local"))
            })
            .unwrap_or_else(|| PathBuf::from("."));
        base.join("RebuildStudio")
    } else {
        let base = non_empty("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| non_empty("HOME").map(|h| PathBuf::from(h).join(".local").join("share")))
            .unwrap_or_else(|| PathBuf::from("."));
        base.join("rebuild-studio")
    }
}

pub fn data_dir() -> PathBuf {
    data_dir_for(cfg!(windows), &|k| std::env::var_os(k))
}

pub fn tools_dir() -> PathBuf {
    match std::env::var_os(ENV_TOOLS).filter(|v| !v.is_empty()) {
        Some(v) => PathBuf::from(v),
        None => data_dir().join("tools"),
    }
}

pub fn controller_json(data_dir: &Path) -> PathBuf {
    data_dir.join("controller.json")
}

pub fn logs_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("logs")
}

pub fn controller_log(data_dir: &Path) -> PathBuf {
    logs_dir(data_dir).join("controller.log")
}

pub fn previews_log_dir(data_dir: &Path) -> PathBuf {
    logs_dir(data_dir).join("previews")
}

pub fn credentials_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("credentials")
}

/// Strip the `\\?\` verbatim prefix Windows `canonicalize` adds (kept for UNC paths).
pub fn strip_verbatim(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    if let Some(rest) = s.strip_prefix(r"\\?\") {
        if !rest.starts_with("UNC\\") {
            return PathBuf::from(rest);
        }
    }
    p.to_path_buf()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<OsString> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| m.get(k).map(OsString::from)
    }

    #[test]
    fn explicit_override_wins() {
        let g = env(&[("REBUILD_STUDIO_DATA", "/x/data"), ("LOCALAPPDATA", "C:\\L")]);
        assert_eq!(data_dir_for(true, &g), PathBuf::from("/x/data"));
        assert_eq!(data_dir_for(false, &g), PathBuf::from("/x/data"));
    }

    #[test]
    fn windows_uses_localappdata() {
        let g = env(&[("LOCALAPPDATA", "C:\\Users\\a\\AppData\\Local")]);
        assert_eq!(
            data_dir_for(true, &g),
            PathBuf::from("C:\\Users\\a\\AppData\\Local").join("RebuildStudio")
        );
    }

    #[test]
    fn windows_falls_back_to_userprofile() {
        let g = env(&[("USERPROFILE", "C:\\Users\\a")]);
        assert_eq!(
            data_dir_for(true, &g),
            PathBuf::from("C:\\Users\\a")
                .join("AppData")
                .join("Local")
                .join("RebuildStudio")
        );
    }

    #[test]
    fn posix_prefers_xdg_then_home() {
        let g = env(&[("XDG_DATA_HOME", "/xdg"), ("HOME", "/home/u")]);
        assert_eq!(data_dir_for(false, &g), PathBuf::from("/xdg/rebuild-studio"));
        let g = env(&[("HOME", "/home/u")]);
        assert_eq!(
            data_dir_for(false, &g),
            PathBuf::from("/home/u/.local/share/rebuild-studio")
        );
        let g = env(&[("XDG_DATA_HOME", ""), ("HOME", "/home/u")]);
        assert_eq!(
            data_dir_for(false, &g),
            PathBuf::from("/home/u/.local/share/rebuild-studio")
        );
    }

    #[test]
    fn verbatim_prefix_is_stripped_except_unc() {
        assert_eq!(strip_verbatim(Path::new(r"\\?\C:\a\b")), PathBuf::from(r"C:\a\b"));
        assert_eq!(
            strip_verbatim(Path::new(r"\\?\UNC\srv\share")),
            PathBuf::from(r"\\?\UNC\srv\share")
        );
        assert_eq!(strip_verbatim(Path::new("/a/b")), PathBuf::from("/a/b"));
    }
}
