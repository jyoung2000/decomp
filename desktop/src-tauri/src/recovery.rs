//! R11: what the shell offers when the controller refuses to start because of the data folder.
//!
//! The controller checks the project database (`PRAGMA quick_check`) before any service starts. When it is damaged
//! the controller writes `<data_dir>/store-recovery.json` (the problem and the verified backups, newest first) and
//! exits with [`EXIT_STORE_DAMAGED`]. The shell then asks the user, in plain words, what to do and restarts the
//! controller with `REBUILD_STUDIO_RECOVER=restore:<backup>` or `=empty`. The controller does the file work: the
//! damaged file is moved to `<data_dir>/damaged/`, never deleted.

use std::path::{Path, PathBuf};

use serde::Deserialize;

/// Controller exit codes (rebuild_controller/store/safety.py).
pub const EXIT_STORE_DAMAGED: i32 = 70;
pub const EXIT_DATA_IN_USE: i32 = 75;
pub const EXIT_PACKAGED: i32 = 78;
/// Environment variable carrying the user's choice to the next controller start.
pub const ENV_RECOVER: &str = "REBUILD_STUDIO_RECOVER";

pub const RESTORE_LABEL: &str = "Restore the last good backup";
pub const EMPTY_LABEL: &str = "Start with an empty project list";
pub const MORE_LABEL: &str = "More options...";
pub const OPEN_LOG_LABEL: &str = "Open log folder";
pub const QUIT_LABEL: &str = "Quit";
pub const BACK_LABEL: &str = "Back";
pub const MORE_TEXT: &str = "Open log folder: shows the data folder; logs\\controller.log is the file to send with a bug report.\n\n\
Quit: closes Rebuild Studio without changing anything; the next start asks again.\n\nBack: return to the choices.";

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct BackupInfo {
    pub name: String,
    pub taken_at: String,
    #[serde(default)]
    pub projects: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct StoreRecovery {
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub backups: Vec<BackupInfo>,
}

pub fn recovery_file(data_dir: &Path) -> PathBuf {
    data_dir.join("store-recovery.json")
}

pub fn read_recovery(data_dir: &Path) -> Option<StoreRecovery> {
    let bytes = std::fs::read(recovery_file(data_dir)).ok()?;
    serde_json::from_slice(&bytes).ok()
}

impl StoreRecovery {
    pub fn latest(&self) -> Option<&BackupInfo> {
        self.backups.first()
    }
}

/// "2026-10-11T03:12:45.123+00:00" -> "2026-10-11 03:12 UTC" (no date library; anything unexpected is shown as is).
pub fn short_time(iso: &str) -> String {
    let b = iso.as_bytes();
    if b.len() >= 16 && b[4] == b'-' && b[7] == b'-' && (b[10] == b'T' || b[10] == b' ') && b[13] == b':' {
        format!("{} {} UTC", &iso[..10], &iso[11..16])
    } else {
        iso.to_string()
    }
}

/// The plain-language text of the first recovery dialog.
pub fn dialog_text(rec: &StoreRecovery, data_dir: &Path) -> String {
    let mut t = String::from(
        "Rebuild Studio cannot open your project list: its database file (studio.sqlite3) is damaged.\n\n\
         Nothing has been deleted. Whatever you choose, the damaged file is kept in the \"damaged\" folder of the data folder, \
         and the output folders of your projects on disk are not touched.\n\n",
    );
    match rec.latest() {
        Some(b) => {
            let projects = b.projects.map(|n| format!(", {n} project{}", if n == 1 { "" } else { "s" })).unwrap_or_default();
            t.push_str(&format!(
                "Restore the last good backup (taken {}{projects}): brings back the project list as it was at that time. \
                 Work recorded after that time is not in it.\n\n",
                short_time(&b.taken_at)
            ));
        }
        None => t.push_str("There is no backup to restore yet.\n\n"),
    }
    t.push_str(
        "Start with an empty project list: Rebuild Studio starts fresh; you can add your projects again.\n\n",
    );
    if rec.latest().is_some() {
        t.push_str("More options: open the log folder, or quit without changing anything.\n\n");
    } else {
        t.push_str("Open log folder: shows the files to send with a bug report. Quit: closes Rebuild Studio without changing anything.\n\n");
    }
    t.push_str(&format!("Data folder: {}", data_dir.display()));
    if !rec.detail.is_empty() {
        let d: String = rec.detail.chars().take(300).collect();
        t.push_str(&format!("\nTechnical detail: {d}"));
    }
    t
}

/// The three buttons of the first dialog (native dialogs offer at most three).
pub fn buttons(rec: &StoreRecovery) -> (String, String, String) {
    match rec.latest() {
        Some(_) => (RESTORE_LABEL.into(), EMPTY_LABEL.into(), MORE_LABEL.into()),
        None => (EMPTY_LABEL.into(), OPEN_LOG_LABEL.into(), QUIT_LABEL.into()),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryAction {
    /// Restart the controller with this `REBUILD_STUDIO_RECOVER` value.
    Recover(String),
    More,
    OpenLog,
    Quit,
}

/// Map a dialog answer to an action. Native dialogs answer Yes/No/Cancel for the three buttons on some platforms
/// and the custom label on others; closing the dialog quits without changing anything.
pub fn recovery_choice(r: &tauri_plugin_dialog::MessageDialogResult, rec: &StoreRecovery) -> RecoveryAction {
    use tauri_plugin_dialog::MessageDialogResult as R;
    let restore = || match rec.latest() {
        Some(b) => RecoveryAction::Recover(format!("restore:{}", b.name)),
        None => RecoveryAction::Quit,
    };
    let empty = || RecoveryAction::Recover("empty".into());
    let has_backup = rec.latest().is_some();
    match r {
        R::Custom(s) if s == RESTORE_LABEL && has_backup => restore(),
        R::Custom(s) if s == EMPTY_LABEL => empty(),
        R::Custom(s) if s == MORE_LABEL && has_backup => RecoveryAction::More,
        R::Custom(s) if s == OPEN_LOG_LABEL => RecoveryAction::OpenLog,
        R::Yes if has_backup => restore(),
        R::Yes => empty(),
        R::No if has_backup => empty(),
        R::No => RecoveryAction::OpenLog,
        _ => RecoveryAction::Quit,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MoreAction {
    OpenLog,
    Quit,
    Back,
}

/// Second dialog ("More options..."): Open log folder / Quit / Back. Closing it goes back.
pub fn more_choice(r: &tauri_plugin_dialog::MessageDialogResult) -> MoreAction {
    use tauri_plugin_dialog::MessageDialogResult as R;
    match r {
        R::Yes => MoreAction::OpenLog,
        R::Custom(s) if s == OPEN_LOG_LABEL => MoreAction::OpenLog,
        R::No => MoreAction::Quit,
        R::Custom(s) if s == QUIT_LABEL => MoreAction::Quit,
        _ => MoreAction::Back,
    }
}

/// Plain-language heading for the other typed exits (shown above the controller's own message).
pub fn exit_heading(code: Option<i32>) -> Option<&'static str> {
    match code {
        Some(EXIT_DATA_IN_USE) => Some(
            "Another Rebuild Studio program is using the project data folder (for example the MCP server an AI client started). \
             Close that program, then choose Try again.",
        ),
        Some(EXIT_PACKAGED) => Some(
            "Rebuild Studio was started from inside another app's Windows package (for example Claude Desktop). Start it from \
             its own Start-menu icon instead.",
        ),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri_plugin_dialog::MessageDialogResult as R;

    fn rec(with_backup: bool) -> StoreRecovery {
        let backups = if with_backup {
            vec![
                BackupInfo { name: "studio-20261011T031245Z-start.sqlite3".into(), taken_at: "2026-10-11T03:12:45.5+00:00".into(), projects: Some(3) },
                BackupInfo { name: "studio-20261010T120000Z-periodic.sqlite3".into(), taken_at: "2026-10-10T12:00:00+00:00".into(), projects: Some(2) },
            ]
        } else {
            vec![]
        };
        StoreRecovery { detail: "database disk image is malformed".into(), backups }
    }

    #[test]
    fn parses_the_controller_recovery_file() {
        let dir = std::env::temp_dir().join(format!("rs-recovery-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(read_recovery(&dir).is_none());
        std::fs::write(
            recovery_file(&dir),
            br#"{"status":"damaged","detail":"x","exit_code":70,"db_path":"d","checked_at":"t",
                "backups":[{"name":"studio-20261011T031245Z-start.sqlite3","taken_at":"2026-10-11T03:12:45+00:00","projects":3,"reason":"start"}]}"#,
        )
        .unwrap();
        let r = read_recovery(&dir).unwrap();
        assert_eq!(r.latest().unwrap().projects, Some(3));
        std::fs::write(recovery_file(&dir), b"{not json").unwrap();
        assert!(read_recovery(&dir).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn restore_choice_names_the_newest_backup() {
        let r = rec(true);
        let want = RecoveryAction::Recover("restore:studio-20261011T031245Z-start.sqlite3".into());
        assert_eq!(recovery_choice(&R::Custom(RESTORE_LABEL.into()), &r), want);
        assert_eq!(recovery_choice(&R::Yes, &r), want);
        assert_eq!(recovery_choice(&R::Custom(EMPTY_LABEL.into()), &r), RecoveryAction::Recover("empty".into()));
        assert_eq!(recovery_choice(&R::No, &r), RecoveryAction::Recover("empty".into()));
        assert_eq!(recovery_choice(&R::Custom(MORE_LABEL.into()), &r), RecoveryAction::More);
        assert_eq!(recovery_choice(&R::Cancel, &r), RecoveryAction::Quit, "closing the dialog changes nothing");
        assert_eq!(buttons(&r).0, RESTORE_LABEL);
    }

    #[test]
    fn without_a_backup_restore_is_not_offered() {
        let r = rec(false);
        assert_eq!(buttons(&r), (EMPTY_LABEL.into(), OPEN_LOG_LABEL.into(), QUIT_LABEL.into()));
        assert_eq!(recovery_choice(&R::Yes, &r), RecoveryAction::Recover("empty".into()));
        assert_eq!(recovery_choice(&R::No, &r), RecoveryAction::OpenLog);
        assert_eq!(recovery_choice(&R::Custom(RESTORE_LABEL.into()), &r), RecoveryAction::Quit);
        assert_eq!(recovery_choice(&R::Custom(QUIT_LABEL.into()), &r), RecoveryAction::Quit);
        assert!(dialog_text(&r, Path::new("C:/d")).contains("no backup"));
    }

    #[test]
    fn more_options_dialog() {
        assert_eq!(more_choice(&R::Yes), MoreAction::OpenLog);
        assert_eq!(more_choice(&R::Custom(OPEN_LOG_LABEL.into())), MoreAction::OpenLog);
        assert_eq!(more_choice(&R::No), MoreAction::Quit);
        assert_eq!(more_choice(&R::Custom(QUIT_LABEL.into())), MoreAction::Quit);
        assert_eq!(more_choice(&R::Cancel), MoreAction::Back);
    }

    #[test]
    fn dialog_text_is_plain_and_names_the_backup_time() {
        let t = dialog_text(&rec(true), Path::new("C:/Users/u/AppData/Local/RebuildStudio"));
        assert!(t.contains("taken 2026-10-11 03:12 UTC, 3 projects"));
        assert!(t.contains("Nothing has been deleted") && t.contains("damaged file is kept"));
        assert!(t.contains("Start with an empty project list"));
        assert!(t.contains("malformed"));
        assert_eq!(short_time("garbage"), "garbage");
    }

    #[test]
    fn typed_exit_headings() {
        assert!(exit_heading(Some(EXIT_DATA_IN_USE)).unwrap().contains("Try again"));
        assert!(exit_heading(Some(EXIT_PACKAGED)).is_some());
        assert!(exit_heading(Some(1)).is_none() && exit_heading(None).is_none());
    }
}
