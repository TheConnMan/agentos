//! Tier-aware observability endpoint seam (issue #460).
//!
//! One shared [`Endpoint`] value type and one [`ObservabilityOutput`] that both
//! tiers return: `commands::observability` (local) and `ops::observability`
//! (cluster) each resolve their surfaces and hand the output back to `main.rs`,
//! which `emit()`s it. The json-vs-human choice is made once, in `Ui::emit`
//! (#456) -- this module never bypasses `CliOutput`.
//!
//! This module is a **leaf**: it must never import `ops` or `local`. The cluster
//! resolver needs ops-private kubectl helpers, so it lives in `ops.rs` and
//! yields these same `Endpoint` values.

use serde_json::Value;

/// A single observability surface.
///
/// The wire key is `name` (not `label`) and the payload key is `surfaces`: this
/// is an **additive superset** of the shipped `{"surfaces":[{"name","url"}]}`
/// contract (PR#503/#456) that agents already consume. `note` and `browsable`
/// are the new fields.
///
/// `url` carries a real, parseable URL when one exists; `note` carries a hint or
/// degraded/error message. They are never overloaded onto one field -- an agent
/// consuming `--json` must be able to parse `url` as a URL, so a degraded
/// endpoint sets `url: None` and puts its message in `note`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub name: String,
    pub url: Option<String>,
    pub note: Option<String>,
    pub browsable: bool,
}

/// Output of `<tier> observability`: the observability surfaces to print, or the
/// `--dry-run` plan of the discovery commands the cluster tier would run. The
/// browser-open side effect happens in the handler (gated by [`should_open`]);
/// this type only carries what `emit` renders.
///
/// An enum rather than a struct because `--dry-run` needs a home: this matches
/// the house pattern of `VersionsOutput`/`KillOutput`, whose dry-run variant
/// delegates to the shared `DryRunPlan` rather than re-deriving its JSON. Only
/// the cluster tier has a `--dry-run` (it shells kubectl); `local observability`
/// is network-free and always yields `Surfaces`.
pub enum ObservabilityOutput {
    /// The `cluster observability --dry-run` plan: `{"dry_run":true,"plan":[..]}`.
    DryRun(crate::ui::DryRunPlan),
    /// The resolved observability surfaces.
    Surfaces(Vec<Endpoint>),
}

impl crate::ui::CliOutput for ObservabilityOutput {
    /// `{"surfaces":[{"name","url":<string|null>,"note":<string|null>,"browsable":bool}]}`,
    /// or the `DryRunPlan`'s own `{"dry_run":true,"plan":[..]}`.
    ///
    /// All four surface keys are ALWAYS emitted, with explicit nulls -- never
    /// conditionally omitted. That is the repo convention pinned by
    /// `kill_output_json_shape_is_pinned` ("the false case must emit
    /// `killed: false`, not omit the key").
    fn to_json(&self) -> Value {
        match self {
            Self::DryRun(plan) => plan.to_json(),
            Self::Surfaces(surfaces) => {
                let rows: Vec<Value> = surfaces
                    .iter()
                    .map(|e| {
                        serde_json::json!({
                            "name": e.name,
                            "url": e.url,
                            "note": e.note,
                            "browsable": e.browsable,
                        })
                    })
                    .collect();
                serde_json::json!({ "surfaces": rows })
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            Self::DryRun(plan) => plan.render(ui),
            Self::Surfaces(surfaces) => {
                for e in surfaces {
                    // A row carries a url or a note, never both meanings in one
                    // field. URLs keep the PR#34 `ui.url` styling.
                    match (&e.url, &e.note) {
                        (Some(url), _) => ui.kv(&e.name, &ui.url(url)),
                        (None, Some(note)) => ui.kv(&e.name, note),
                        (None, None) => ui.kv(&e.name, "unavailable"),
                    }
                }
            }
        }
    }
}

/// Local Curie Console (browsable). Port literal lives once, here;
/// `local.rs::ENDPOINTS` references it.
pub const LOCAL_CONSOLE_URL: &str = "http://localhost:28080/?api=1";
/// Local Langfuse UI (browsable).
pub const LOCAL_LANGFUSE_URL: &str = "http://localhost:23000";
/// Local platform API base (not browsable -- an agent target, not a webapp).
pub const LOCAL_API_URL: &str = "http://localhost:28000";

/// The local tier's three observability surfaces: Console + Langfuse
/// (browsable) and the API base (not browsable).
pub fn local_endpoints() -> Vec<Endpoint> {
    vec![
        Endpoint {
            name: "Curie Console".to_string(),
            url: Some(LOCAL_CONSOLE_URL.to_string()),
            note: None,
            browsable: true,
        },
        Endpoint {
            name: "Langfuse UI (traces / cost / evals)".to_string(),
            url: Some(LOCAL_LANGFUSE_URL.to_string()),
            note: None,
            browsable: true,
        },
        Endpoint {
            name: "Curie API".to_string(),
            url: Some(LOCAL_API_URL.to_string()),
            note: None,
            browsable: false,
        },
    ]
}

/// Whether the handler should open `e` in a browser:
/// `e.browsable && open && !json`.
///
/// The `!json` term is belt-and-suspenders: `--open` is already an explicit
/// opt-in, but a machine consumer must never have tabs spawned at it. The base
/// already guaranteed "never open under `--json`"; #460 PRESERVES that across
/// both tiers rather than re-deriving it.
pub fn should_open(e: &Endpoint, open: bool, json: bool) -> bool {
    e.browsable && open && !json
}

/// Best-effort browser open of every surface [`should_open`] selects, shared by
/// both tiers so the opener choice and the swallow-the-failure policy cannot
/// drift. A missing opener (headless host, CI) is NOT an error: the URLs are
/// printed by `render` either way.
pub async fn open_endpoints(endpoints: &[Endpoint], open: bool, json: bool) {
    if open && json {
        // Surfaced rather than silently dropped, on stderr so the `--json`
        // stdout payload stays a single clean JSON line.
        crate::ui::ui().warn("--json suppresses --open: no browser was opened");
    }
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    for e in endpoints {
        if !should_open(e, open, json) {
            continue;
        }
        let Some(url) = e.url.as_deref() else {
            continue;
        };
        let _ = tokio::process::Command::new(opener)
            .arg(url)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ep(name: &str, url: Option<&str>, browsable: bool) -> Endpoint {
        Endpoint {
            name: name.to_string(),
            url: url.map(str::to_string),
            note: None,
            browsable,
        }
    }

    #[test]
    fn local_endpoints_yields_console_langfuse_and_api() {
        let eps = local_endpoints();
        assert_eq!(eps.len(), 3, "local tier yields exactly three surfaces");

        // Console: browsable, exact URL, no note. The name matches the shipped
        // handler's so the local payload stays a superset, not a rename.
        assert_eq!(eps[0].name, "Curie Console");
        assert_eq!(eps[0].url.as_deref(), Some(LOCAL_CONSOLE_URL));
        assert_eq!(eps[0].url.as_deref(), Some("http://localhost:28080/?api=1"));
        assert_eq!(eps[0].note, None);
        assert!(eps[0].browsable);

        // Langfuse: browsable, exact URL, no note.
        assert_eq!(eps[1].name, "Langfuse UI (traces / cost / evals)");
        assert_eq!(eps[1].url.as_deref(), Some(LOCAL_LANGFUSE_URL));
        assert_eq!(eps[1].url.as_deref(), Some("http://localhost:23000"));
        assert_eq!(eps[1].note, None);
        assert!(eps[1].browsable);

        // API base: has a URL but is NOT browsable (an agent target).
        assert_eq!(eps[2].name, "Curie API");
        assert_eq!(eps[2].url.as_deref(), Some(LOCAL_API_URL));
        assert_eq!(eps[2].url.as_deref(), Some("http://localhost:28000"));
        assert_eq!(eps[2].note, None);
        assert!(
            !eps[2].browsable,
            "the API base is never opened in a browser"
        );
    }

    /// The full browsable x open x json truth table: `browsable && open && !json`.
    /// Exactly one of the eight combinations may open a browser.
    #[test]
    fn should_open_truth_table() {
        let yes = ep("Curie Console", Some(LOCAL_CONSOLE_URL), true);
        let no = ep("Curie API", Some(LOCAL_API_URL), false);

        // browsable = true: only --open without --json opens.
        assert!(should_open(&yes, true, false), "browsable+open+!json opens");
        assert!(!should_open(&yes, true, true), "--json wins over --open");
        assert!(!should_open(&yes, false, false), "no --open, no browser");
        assert!(!should_open(&yes, false, true), "neither open nor json");

        // browsable = false: never, in any combination.
        assert!(!should_open(&no, true, false));
        assert!(!should_open(&no, true, true));
        assert!(!should_open(&no, false, false));
        assert!(!should_open(&no, false, true));
    }
}
