//! Terminal rendering: per-event output lines and the boxed `start` summary.
//!
//! The boxed summary follows the design canon (claude-design-prompt.md): a
//! Supabase-style rounded box listing the local bot URL, the emulator and eval
//! commands, and the version line.

use agentos_aci_protocol::{OutboundEvent, SessionStatus};

/// Human-readable session status, matching the wire vocabulary.
pub fn status_str(status: &SessionStatus) -> &'static str {
    match status {
        SessionStatus::Done => "done",
        SessionStatus::IdleAwaitingInput => "idle-awaiting-input",
        SessionStatus::ClassifiedFailure => "classified-failure",
    }
}

/// Renders the outbound events of one turn into printable lines.
///
/// Text deltas print as-is. Tool notes and side-effect flags print as dim
/// arrow/bang lines. The final frame prints a status trailer; its text is
/// repeated only when nothing was streamed before it (so a turn that only
/// yields a final still shows its answer, without duplicating streamed text).
#[derive(Default)]
pub struct TurnPrinter {
    streamed_text: bool,
}

impl TurnPrinter {
    /// The printable line for one event, if any.
    pub fn line_for(&mut self, event: &OutboundEvent) -> Option<String> {
        match event {
            OutboundEvent::TextDelta { text, .. } => {
                self.streamed_text = true;
                Some(text.clone())
            }
            OutboundEvent::ToolNote { text, tool, .. } => match tool {
                Some(tool) => Some(format!("  -> [{tool}] {text}")),
                None => Some(format!("  -> {text}")),
            },
            OutboundEvent::SideEffectFlag { tool, detail, .. } => {
                let tool = tool.as_deref().unwrap_or("unknown tool");
                let detail = detail
                    .as_deref()
                    .map(|d| format!(": {d}"))
                    .unwrap_or_default();
                Some(format!("  !  side effect via {tool}{detail}"))
            }
            OutboundEvent::ErrorEvent {
                message,
                classification,
                ..
            } => {
                let class = classification
                    .as_deref()
                    .map(|c| format!(" [{c}]"))
                    .unwrap_or_default();
                Some(format!("error{class}: {message}"))
            }
            OutboundEvent::Final { text, status, .. } => {
                let trailer = format!("-- final ({})", status_str(status));
                if self.streamed_text || text.is_empty() {
                    Some(trailer)
                } else {
                    Some(format!("{text}\n{trailer}"))
                }
            }
        }
    }
}

/// Render the boxed environment summary the design specifies for `agentos start`.
pub fn boxed_summary(title: &str, rows: &[(&str, String)]) -> String {
    let label_width = rows.iter().map(|(label, _)| label.len()).max().unwrap_or(0);
    let body: Vec<String> = rows
        .iter()
        .map(|(label, value)| format!("  {label:<label_width$}   {value}"))
        .collect();
    // Inner width: the character count between the two side bars. The title
    // head occupies "- <title> " (3 + title) on the top rule, plus at least a
    // couple of trailing rule characters so the box always closes with a rule.
    let inner = body
        .iter()
        .map(String::len)
        .max()
        .unwrap_or(0)
        .max(title.len() + 5)
        + 2;

    let mut out = String::new();
    out.push('\u{256d}'); // rounded top-left
    out.push_str(&format!("\u{2500} {title} "));
    out.push_str(&"\u{2500}".repeat(inner.saturating_sub(title.len() + 3)));
    out.push('\u{256e}');
    out.push('\n');
    for line in &body {
        out.push('\u{2502}');
        out.push_str(&format!("{line:<inner$}"));
        out.push('\u{2502}');
        out.push('\n');
    }
    out.push('\u{2570}');
    out.push_str(&"\u{2500}".repeat(inner));
    out.push('\u{256f}');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_aci_protocol::PROTOCOL_VERSION;

    fn v() -> String {
        PROTOCOL_VERSION.to_string()
    }

    #[test]
    fn streams_deltas_then_summarizes_final_without_repeating_text() {
        let mut printer = TurnPrinter::default();
        let delta = OutboundEvent::TextDelta {
            version: v(),
            text: "all done".into(),
        };
        let final_frame = OutboundEvent::Final {
            version: v(),
            text: "all done".into(),
            status: SessionStatus::Done,
        };
        assert_eq!(printer.line_for(&delta).unwrap(), "all done");
        assert_eq!(printer.line_for(&final_frame).unwrap(), "-- final (done)");
    }

    #[test]
    fn prints_final_text_when_nothing_was_streamed() {
        let mut printer = TurnPrinter::default();
        let final_frame = OutboundEvent::Final {
            version: v(),
            text: "quiet answer".into(),
            status: SessionStatus::IdleAwaitingInput,
        };
        assert_eq!(
            printer.line_for(&final_frame).unwrap(),
            "quiet answer\n-- final (idle-awaiting-input)"
        );
    }

    #[test]
    fn renders_tool_notes_side_effects_and_errors() {
        let mut printer = TurnPrinter::default();
        let note = OutboundEvent::ToolNote {
            version: v(),
            text: "running echo hi".into(),
            tool: Some("Bash".into()),
        };
        let flag = OutboundEvent::SideEffectFlag {
            version: v(),
            tool: Some("Bash".into()),
            detail: None,
        };
        let error = OutboundEvent::ErrorEvent {
            version: v(),
            message: "boom".into(),
            classification: Some("budget".into()),
        };
        assert_eq!(
            printer.line_for(&note).unwrap(),
            "  -> [Bash] running echo hi"
        );
        assert_eq!(
            printer.line_for(&flag).unwrap(),
            "  !  side effect via Bash"
        );
        assert_eq!(printer.line_for(&error).unwrap(), "error [budget]: boom");
    }

    #[test]
    fn boxed_summary_lines_are_flush() {
        let rows = [
            ("Local bot", "http://localhost:7245".to_string()),
            ("Slack emulator", "agentos send \"<message>\"".to_string()),
            ("Eval runner", "agentos eval".to_string()),
            ("Version", "dev @ 4f2c91a".to_string()),
        ];
        let boxed = boxed_summary("agentos dev environment", &rows);
        let lines: Vec<&str> = boxed.lines().collect();
        assert_eq!(lines.len(), rows.len() + 2);
        let width = lines[0].chars().count();
        for line in &lines {
            assert_eq!(line.chars().count(), width, "misaligned line: {line}");
        }
        assert!(lines[0].contains("agentos dev environment"));
        assert!(lines[1].contains("Local bot"));
        assert!(lines[1].contains("http://localhost:7245"));
    }
}
