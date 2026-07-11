//! Semantic exit codes and error classification for the agent-facing CLI
//! contract (ADR-0021 decision 1).
//!
//! An agent driving `agentos` needs to branch on *why* a command failed without
//! parsing prose. The scheme is four stable exit classes:
//!
//! - `0` Success: the command did what was asked.
//! - `1` Failure: a genuine runtime failure (the request was well-formed but the
//!   operation did not succeed).
//! - `2` Usage: a deterministic input error (a missing `--yes`, a malformed
//!   flag) -- retrying the same argv will fail identically, so fix the input.
//! - `3` Transient: a retryable condition (the endpoint was unreachable or timed
//!   out) -- the same argv may succeed once the dependency is up.
//!
//! A command tags an input error by returning [`usage`] (or building a
//! [`CliError`] directly); an unreachable dependency is detected structurally by
//! walking the error chain for a `reqwest` connect/timeout error. Everything
//! else is [`ExitClass::Failure`]. [`classify`] returns the class plus an
//! optional one-line fix hint, and [`error_json`] renders the whole thing as the
//! `--json` error payload.

/// The four semantic exit classes. The `#[repr(i32)]` values are the process
/// exit codes and are a stable contract agents branch on.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(i32)]
pub enum ExitClass {
    Success = 0,
    Failure = 1,
    Usage = 2,
    Transient = 3,
}

impl ExitClass {
    /// The process exit code for this class.
    pub fn code(self) -> i32 {
        self as i32
    }
}

/// A tagged CLI error: a message, an optional actionable fix hint, and the exit
/// class it maps to. Carried through `anyhow`'s chain so [`classify`] can recover
/// the class even when the error was wrapped in later context.
#[derive(Debug)]
pub struct CliError {
    pub message: String,
    pub fix: Option<String>,
    pub class: ExitClass,
}

impl CliError {
    /// A deterministic input error (exit 2).
    pub fn usage(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Usage,
        }
    }

    /// A retryable condition (exit 3).
    pub fn transient(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Transient,
        }
    }

    /// Attach an actionable fix hint (surfaced in the `--json` payload).
    pub fn with_fix(mut self, fix: impl Into<String>) -> Self {
        self.fix = Some(fix.into());
        self
    }
}

impl std::fmt::Display for CliError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Render the message only; the fix travels through `classify`, not the
        // Display surface, so a wrapping `err.to_string()` stays clean.
        f.write_str(&self.message)
    }
}

impl std::error::Error for CliError {}

/// Build a usage error (exit 2) as an `anyhow::Error` ready to `return Err(..)`.
pub fn usage(msg: impl Into<String>) -> anyhow::Error {
    anyhow::Error::from(CliError::usage(msg))
}

/// Build a transient error (exit 3) as an `anyhow::Error` ready to `return Err(..)`.
pub fn transient(msg: impl Into<String>) -> anyhow::Error {
    anyhow::Error::from(CliError::transient(msg))
}

/// Classify an error into its exit class plus an optional fix hint. Walks the
/// `anyhow` chain so a tagged [`CliError`] is found even under context layers; a
/// `reqwest` connect/timeout failure anywhere in the chain maps to
/// [`ExitClass::Transient`] with a retry hint; everything else is
/// [`ExitClass::Failure`] with no fix.
pub fn classify(err: &anyhow::Error) -> (ExitClass, Option<String>) {
    for cause in err.chain() {
        if let Some(cli) = cause.downcast_ref::<CliError>() {
            // A transient error is retryable by definition, so it always carries
            // a retry hint even when the caller did not attach a specific one.
            let fix = cli
                .fix
                .clone()
                .or_else(|| (cli.class == ExitClass::Transient).then(|| RETRY_HINT.to_string()));
            return (cli.class, fix);
        }
    }
    if is_transient_reqwest(err) {
        return (ExitClass::Transient, Some(RETRY_HINT.to_string()));
    }
    (ExitClass::Failure, None)
}

/// True when the error chain contains a `reqwest` connect/timeout failure --
/// i.e. a dependency (runner, platform API) was unreachable rather than
/// returning an error status. The single definition of "unreachable" shared by
/// [`classify`]'s Transient rule and command-level remediation hints, so the
/// two never diverge on what counts as retryable.
pub fn is_transient_reqwest(err: &anyhow::Error) -> bool {
    err.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .is_some_and(|e| e.is_connect() || e.is_timeout())
    })
}

/// The default one-line retry hint for a transient (retryable) failure.
const RETRY_HINT: &str = "the endpoint was unreachable; retry once it is up";

/// The `--json` error payload: `{"error": <message>, "fix": <hint or null>}`.
/// `error` is the top-level rendered error; `fix` comes from [`classify`].
pub fn error_json(err: &anyhow::Error) -> serde_json::Value {
    let (_class, fix) = classify(err);
    serde_json::json!({
        "error": format!("{err:#}"),
        "fix": fix,
    })
}
