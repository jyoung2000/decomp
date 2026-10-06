//! Error type returned to the webview. Shape mirrors the controller API error
//! (`{code, message}`), so the UI can render both the same way.

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CmdError {
    pub code: &'static str,
    pub message: String,
}

impl CmdError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new("invalid_argument", message)
    }
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new("not_found", message)
    }
    pub fn io(context: &str, e: &std::io::Error) -> Self {
        Self::new("io_error", format!("{context}: {e}"))
    }
}

impl std::fmt::Display for CmdError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for CmdError {}

pub type CmdResult<T> = Result<T, CmdError>;
