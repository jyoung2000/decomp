//! Secret storage.
//!
//! * Windows: Windows Credential Manager (`CredWriteW` / `CredReadW` / `CredDeleteW`),
//!   generic credentials persisted for the local machine, target `RebuildStudio:<name>`.
//!   Reported as `backend: "windows-credential-manager"`.
//! * Every other OS: a `0600` file under `<data_dir>/credentials/` (directory `0700`).
//!   NOT encrypted at rest; reported as `backend: "file-fallback"` so the UI can say so.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{CmdError, CmdResult};

pub const BACKEND_WINDOWS: &str = "windows-credential-manager";
pub const BACKEND_FILE: &str = "file-fallback";
/// CRED_MAX_CREDENTIAL_BLOB_SIZE
pub const MAX_SECRET_BYTES: usize = 2560;

pub fn backend_name() -> &'static str {
    if cfg!(windows) {
        BACKEND_WINDOWS
    } else {
        BACKEND_FILE
    }
}

pub fn validate_name(name: &str) -> CmdResult<()> {
    let ok = !name.is_empty()
        && name.len() <= 128
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '/' | '@' | '-'));
    if ok {
        Ok(())
    } else {
        Err(CmdError::invalid(
            "credential name must be 1-128 chars of [A-Za-z0-9._:/@-]",
        ))
    }
}

fn validate_secret(secret: &str) -> CmdResult<()> {
    if secret.len() > MAX_SECRET_BYTES {
        return Err(CmdError::invalid(format!(
            "secret too large ({} bytes, max {MAX_SECRET_BYTES})",
            secret.len()
        )));
    }
    Ok(())
}

pub fn set(data_dir: &Path, name: &str, secret: &str) -> CmdResult<&'static str> {
    validate_name(name)?;
    validate_secret(secret)?;
    #[cfg(windows)]
    {
        let _ = data_dir;
        wincred::write(name, secret)?;
        Ok(BACKEND_WINDOWS)
    }
    #[cfg(not(windows))]
    {
        file::write(data_dir, name, secret)?;
        Ok(BACKEND_FILE)
    }
}

pub fn get(data_dir: &Path, name: &str) -> CmdResult<(Option<String>, &'static str)> {
    validate_name(name)?;
    #[cfg(windows)]
    {
        let _ = data_dir;
        Ok((wincred::read(name)?, BACKEND_WINDOWS))
    }
    #[cfg(not(windows))]
    {
        Ok((file::read(data_dir, name)?, BACKEND_FILE))
    }
}

pub fn delete(data_dir: &Path, name: &str) -> CmdResult<(bool, &'static str)> {
    validate_name(name)?;
    #[cfg(windows)]
    {
        let _ = data_dir;
        Ok((wincred::delete(name)?, BACKEND_WINDOWS))
    }
    #[cfg(not(windows))]
    {
        Ok((file::delete(data_dir, name)?, BACKEND_FILE))
    }
}

// ---------------------------------------------------------------------------
// File fallback (compiled everywhere so it is unit-testable on Windows too)
// ---------------------------------------------------------------------------

#[cfg_attr(windows, allow(dead_code))]
pub(crate) mod file {
    use super::*;
    use std::io::Write;

    #[derive(Serialize, Deserialize)]
    struct Stored {
        name: String,
        secret: String,
        updated_at_ms: u64,
    }

    fn hex(name: &str) -> String {
        name.bytes().map(|b| format!("{b:02x}")).collect()
    }

    fn path_for(data_dir: &Path, name: &str) -> PathBuf {
        crate::paths::credentials_dir(data_dir).join(format!("{}.cred", hex(name)))
    }

    fn ensure_dir(dir: &Path) -> std::io::Result<()> {
        std::fs::create_dir_all(dir)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }

    #[cfg(test)]
    pub fn path_for_test(data_dir: &Path, name: &str) -> PathBuf {
        path_for(data_dir, name)
    }

    pub fn write(data_dir: &Path, name: &str, secret: &str) -> CmdResult<()> {
        let dir = crate::paths::credentials_dir(data_dir);
        ensure_dir(&dir).map_err(|e| CmdError::io("create credentials dir", &e))?;
        let target = path_for(data_dir, name);
        let tmp = target.with_extension("tmp");
        let body = serde_json::to_vec(&Stored {
            name: name.to_string(),
            secret: secret.to_string(),
            updated_at_ms: crate::procs::now_ms(),
        })
        .map_err(|e| CmdError::new("encode_error", e.to_string()))?;
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts
            .open(&tmp)
            .map_err(|e| CmdError::io("write credential", &e))?;
        f.write_all(&body)
            .and_then(|_| f.sync_all())
            .map_err(|e| CmdError::io("write credential", &e))?;
        drop(f);
        std::fs::rename(&tmp, &target).map_err(|e| CmdError::io("commit credential", &e))
    }

    pub fn read(data_dir: &Path, name: &str) -> CmdResult<Option<String>> {
        match std::fs::read(path_for(data_dir, name)) {
            Ok(bytes) => {
                let s: Stored = serde_json::from_slice(&bytes)
                    .map_err(|e| CmdError::new("corrupt_credential", e.to_string()))?;
                Ok(Some(s.secret))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(CmdError::io("read credential", &e)),
        }
    }

    pub fn delete(data_dir: &Path, name: &str) -> CmdResult<bool> {
        match std::fs::remove_file(path_for(data_dir, name)) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(CmdError::io("delete credential", &e)),
        }
    }
}

// ---------------------------------------------------------------------------
// Windows Credential Manager
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod wincred {
    use super::*;
    use windows::core::{PCWSTR, PWSTR};
    use windows::Win32::Foundation::ERROR_NOT_FOUND;
    use windows::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_FLAGS,
        CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };

    fn target(name: &str) -> Vec<u16> {
        format!("RebuildStudio:{name}")
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect()
    }

    fn map_err(context: &str, e: windows::core::Error) -> CmdError {
        CmdError::new("credential_manager_error", format!("{context}: {e}"))
    }

    pub fn write(name: &str, secret: &str) -> CmdResult<()> {
        let mut target = target(name);
        let mut user: Vec<u16> = "RebuildStudio".encode_utf16().chain(std::iter::once(0)).collect();
        let mut blob = secret.as_bytes().to_vec();
        let cred = CREDENTIALW {
            Flags: CRED_FLAGS(0),
            Type: CRED_TYPE_GENERIC,
            TargetName: PWSTR(target.as_mut_ptr()),
            CredentialBlobSize: blob.len() as u32,
            CredentialBlob: blob.as_mut_ptr(),
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            UserName: PWSTR(user.as_mut_ptr()),
            ..Default::default()
        };
        // SAFETY: `cred` points at buffers that outlive the call; CredWriteW copies them.
        unsafe { CredWriteW(&cred, 0) }.map_err(|e| map_err("CredWriteW", e))
    }

    pub fn read(name: &str) -> CmdResult<Option<String>> {
        let target = target(name);
        let mut out: *mut CREDENTIALW = std::ptr::null_mut();
        // SAFETY: on success `out` is a CredFree-able buffer we release below.
        let res = unsafe { CredReadW(PCWSTR(target.as_ptr()), CRED_TYPE_GENERIC, None, &mut out) };
        match res {
            Ok(()) => {
                let secret = unsafe {
                    let c = &*out;
                    let bytes = if c.CredentialBlob.is_null() || c.CredentialBlobSize == 0 {
                        &[][..]
                    } else {
                        std::slice::from_raw_parts(c.CredentialBlob, c.CredentialBlobSize as usize)
                    };
                    let s = String::from_utf8_lossy(bytes).into_owned();
                    CredFree(out as *const core::ffi::c_void);
                    s
                };
                Ok(Some(secret))
            }
            Err(e) if e.code() == ERROR_NOT_FOUND.to_hresult() => Ok(None),
            Err(e) => Err(map_err("CredReadW", e)),
        }
    }

    pub fn delete(name: &str) -> CmdResult<bool> {
        let target = target(name);
        match unsafe { CredDeleteW(PCWSTR(target.as_ptr()), CRED_TYPE_GENERIC, None) } {
            Ok(()) => Ok(true),
            Err(e) if e.code() == ERROR_NOT_FOUND.to_hresult() => Ok(false),
            Err(e) => Err(map_err("CredDeleteW", e)),
        }
    }

    /// Delete every generic credential whose target starts with `RebuildStudio:`. Returns how many were removed.
    pub fn delete_all() -> CmdResult<usize> {
        use windows::Win32::Security::Credentials::{CredEnumerateW, CRED_ENUMERATE_FLAGS};
        let filter: Vec<u16> = "RebuildStudio:*".encode_utf16().chain(std::iter::once(0)).collect();
        let mut count = 0u32;
        let mut list: *mut *mut CREDENTIALW = std::ptr::null_mut();
        // SAFETY: on success `list` is a CredFree-able array of `count` credential pointers.
        let res = unsafe { CredEnumerateW(PCWSTR(filter.as_ptr()), Some(CRED_ENUMERATE_FLAGS(0)), &mut count, &mut list) };
        let targets: Vec<Vec<u16>> = match res {
            Ok(()) => unsafe {
                let items = std::slice::from_raw_parts(list, count as usize);
                let t = items
                    .iter()
                    .filter(|c| (***c).Type == CRED_TYPE_GENERIC)
                    .map(|c| {
                        let mut w = (**c).TargetName.as_wide().to_vec();
                        w.push(0);
                        w
                    })
                    .collect();
                CredFree(list as *const core::ffi::c_void);
                t
            },
            Err(e) if e.code() == ERROR_NOT_FOUND.to_hresult() => return Ok(0),
            Err(e) => return Err(map_err("CredEnumerateW", e)),
        };
        let mut removed = 0;
        for t in targets {
            if unsafe { CredDeleteW(PCWSTR(t.as_ptr()), CRED_TYPE_GENERIC, None) }.is_ok() {
                removed += 1;
            }
        }
        Ok(removed)
    }
}

/// Remove every stored Rebuild Studio secret (Credential Manager and file fallback). Used by the uninstaller
/// when the user explicitly asks to delete application data.
pub fn delete_all(data_dir: &Path) -> usize {
    let mut n = 0;
    #[cfg(windows)]
    {
        n += wincred::delete_all().unwrap_or(0);
    }
    let dir = crate::paths::credentials_dir(data_dir);
    if dir.is_dir() {
        n += std::fs::read_dir(&dir).map(|it| it.count()).unwrap_or(0);
        let _ = std::fs::remove_dir_all(&dir);
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("rs-cred-{tag}-{}", crate::procs::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn name_validation() {
        assert!(validate_name("openai:default").is_ok());
        assert!(validate_name("").is_err());
        assert!(validate_name("a\\b").is_err());
        assert!(validate_name("a b").is_err());
        assert!(validate_name(&"x".repeat(129)).is_err());
    }

    #[test]
    fn file_backend_roundtrip_and_permissions() {
        let d = tmp("rt");
        file::write(&d, "anthropic:main", "sk-test-123").unwrap();
        assert_eq!(file::read(&d, "anthropic:main").unwrap().as_deref(), Some("sk-test-123"));
        file::write(&d, "anthropic:main", "sk-new").unwrap();
        assert_eq!(file::read(&d, "anthropic:main").unwrap().as_deref(), Some("sk-new"));
        assert_eq!(file::read(&d, "other").unwrap(), None);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let p = file::path_for_test(&d, "anthropic:main");
            assert_eq!(std::fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
            let dir = crate::paths::credentials_dir(&d);
            assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        }
        assert!(file::delete(&d, "anthropic:main").unwrap());
        assert!(!file::delete(&d, "anthropic:main").unwrap());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn path_traversal_names_never_escape_the_credentials_dir() {
        let d = tmp("trav");
        // validate_name allows '/' and '.', but the hex-encoded file name cannot traverse.
        file::write(&d, "../../x", "s").unwrap();
        let p = file::path_for_test(&d, "../../x");
        assert!(p.starts_with(crate::paths::credentials_dir(&d)));
        let stem = p.file_stem().unwrap().to_string_lossy().into_owned();
        assert!(stem.chars().all(|c| c.is_ascii_hexdigit()), "{stem}");
        let _ = std::fs::remove_dir_all(&d);
    }

    /// Gate W6 (docs/WINDOWS_RELEASE_GATES.md): the real Windows Credential Manager, through the same
    /// `set`/`get`/`delete` the `credential_*` Tauri commands call. Windows-only; never runs on the Linux host.
    #[cfg(windows)]
    #[test]
    fn windows_credential_manager_roundtrip() {
        struct Cleanup(PathBuf, String);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = delete(&self.0, &self.1);
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let d = tmp("wincred");
        let name = format!("rs-test.{}.{}", std::process::id(), crate::procs::now_ms());
        let _cleanup = Cleanup(d.clone(), name.clone());

        assert_eq!(get(&d, &name).unwrap(), (None, BACKEND_WINDOWS), "fresh name must be absent");
        let secret = "sk-test-\u{e4}\u{f6}\u{fc}-\u{2713}-0123456789";
        assert_eq!(set(&d, &name, secret).unwrap(), BACKEND_WINDOWS);
        assert_eq!(get(&d, &name).unwrap(), (Some(secret.to_string()), BACKEND_WINDOWS));
        assert_eq!(set(&d, &name, "second-value").unwrap(), BACKEND_WINDOWS);
        assert_eq!(get(&d, &name).unwrap().0.as_deref(), Some("second-value"));
        // Nothing may fall back to files under the data dir on Windows.
        assert!(!crate::paths::credentials_dir(&d).exists(), "Windows must not use the file fallback");
        assert_eq!(delete(&d, &name).unwrap(), (true, BACKEND_WINDOWS));
        assert_eq!(get(&d, &name).unwrap().0, None);
        assert_eq!(delete(&d, &name).unwrap(), (false, BACKEND_WINDOWS));
    }

    #[test]
    fn delete_all_removes_every_studio_secret_only() {
        let d = tmp("delall");
        set(&d, "rs-test-delall-a", "s1").unwrap();
        set(&d, "rs-test-delall-b", "s2").unwrap();
        assert!(delete_all(&d) >= 2);
        assert_eq!(get(&d, "rs-test-delall-a").unwrap().0, None);
        assert_eq!(get(&d, "rs-test-delall-b").unwrap().0, None);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn public_api_reports_backend_and_rejects_oversized() {
        let d = tmp("api");
        let b = set(&d, "k", "v");
        #[cfg(not(windows))]
        {
            assert_eq!(b.unwrap(), "file-fallback");
            assert_eq!(get(&d, "k").unwrap(), (Some("v".to_string()), "file-fallback"));
            assert_eq!(delete(&d, "k").unwrap(), (true, "file-fallback"));
        }
        #[cfg(windows)]
        {
            let _ = b;
        }
        assert_eq!(
            set(&d, "k", &"x".repeat(MAX_SECRET_BYTES + 1)).unwrap_err().code,
            "invalid_argument"
        );
        let _ = std::fs::remove_dir_all(&d);
    }
}
