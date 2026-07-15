//! Terminal adapter for the rendering-neutral `agentos-reply` channel contract.

use serde::Deserialize;

pub const REPLY_FENCE: &str = "```agentos-reply";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TerminalAction {
    pub label: String,
    pub value: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TerminalMessage {
    pub lines: Vec<String>,
    pub actions: Vec<TerminalAction>,
    pub action_prompt: Option<String>,
    pub allow_free_text: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Action {
    label: String,
    value: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageField {
    label: String,
    value: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageLink {
    label: String,
    url: String,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
enum Interaction {
    #[serde(rename = "choice")]
    Choice {
        id: String,
        prompt: Option<String>,
        options: Vec<Action>,
        #[serde(default = "default_true")]
        allow_free_text: bool,
    },
    #[serde(rename = "confirm")]
    Confirm {
        id: String,
        prompt: String,
        confirm: Action,
        cancel: Action,
        #[serde(default)]
        allow_free_text: bool,
    },
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OutboundMessage {
    version: String,
    text: String,
    status: Option<String>,
    header: Option<String>,
    #[serde(default)]
    fields: Vec<MessageField>,
    #[serde(default)]
    links: Vec<MessageLink>,
    footer: Option<String>,
    interaction: Option<Interaction>,
}

#[derive(Debug, Deserialize)]
struct LegacyMessage {
    #[serde(default)]
    text: String,
    status: Option<String>,
    header: Option<String>,
    #[serde(default)]
    fields: Vec<(String, String)>,
    #[serde(default)]
    buttons: Vec<(String, String)>,
    #[serde(default)]
    links: Vec<(String, String)>,
    footer: Option<String>,
}

fn display_lines(
    text: String,
    status: Option<String>,
    header: Option<String>,
    fields: impl IntoIterator<Item = (String, String)>,
    links: impl IntoIterator<Item = (String, String)>,
    footer: Option<String>,
) -> Vec<String> {
    let mut lines = Vec::new();
    if let Some(status) = status.filter(|value| !value.is_empty()) {
        lines.push(status);
    }
    if let Some(header) = header.filter(|value| !value.is_empty()) {
        lines.push(header);
    }
    for (label, value) in fields {
        lines.push(format!("{label}: {value}"));
    }
    lines.extend(text.lines().map(str::to_string));
    for (label, url) in links {
        lines.push(format!("{label}: {url}"));
    }
    if let Some(footer) = footer.filter(|value| !value.is_empty()) {
        lines.push(footer);
    }
    lines
}

/// Parse a complete fenced semantic message. Legacy button pairs remain readable
/// during migration, but only the versioned shape is the public contract.
pub fn parse_terminal_message(text: &str) -> Option<TerminalMessage> {
    let start = text.find(REPLY_FENCE)? + REPLY_FENCE.len();
    let rest = text
        .get(start..)?
        .strip_prefix('\n')
        .unwrap_or(&text[start..]);
    let end = rest.rfind("```")?;
    let payload = rest[..end].trim();
    let value: serde_json::Value = serde_json::from_str(payload).ok()?;
    if value.get("version").is_some() {
        let message: OutboundMessage = serde_json::from_value(value).ok()?;
        if message.version != "1.0" {
            return None;
        }
        let (actions, prompt, allow_free_text) = match message.interaction {
            Some(Interaction::Choice {
                id,
                prompt,
                options,
                allow_free_text,
            }) if !id.is_empty() && !options.is_empty() => (
                options
                    .into_iter()
                    .map(|action| TerminalAction {
                        label: action.label,
                        value: action.value,
                    })
                    .collect(),
                prompt,
                allow_free_text,
            ),
            Some(Interaction::Confirm {
                id,
                prompt,
                confirm,
                cancel,
                allow_free_text,
            }) if !id.is_empty() => (
                vec![
                    TerminalAction {
                        label: confirm.label,
                        value: confirm.value,
                    },
                    TerminalAction {
                        label: cancel.label,
                        value: cancel.value,
                    },
                ],
                Some(prompt),
                allow_free_text,
            ),
            _ => (Vec::new(), None, true),
        };
        return Some(TerminalMessage {
            lines: display_lines(
                message.text,
                message.status,
                message.header,
                message
                    .fields
                    .into_iter()
                    .map(|item| (item.label, item.value)),
                message.links.into_iter().map(|item| (item.label, item.url)),
                message.footer,
            ),
            actions,
            action_prompt: prompt,
            allow_free_text,
        });
    }

    let legacy: LegacyMessage = serde_json::from_value(value).ok()?;
    Some(TerminalMessage {
        lines: display_lines(
            legacy.text,
            legacy.status,
            legacy.header,
            legacy.fields,
            legacy.links,
            legacy.footer,
        ),
        actions: legacy
            .buttons
            .into_iter()
            .map(|(label, value)| TerminalAction { label, value })
            .collect(),
        action_prompt: None,
        allow_free_text: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn choice_renders_labels_but_preserves_values() {
        let message = parse_terminal_message(
            "```agentos-reply\n{\"version\":\"1.0\",\"text\":\"Pick one\",\"interaction\":{\"kind\":\"choice\",\"id\":\"repo\",\"prompt\":\"Repository\",\"options\":[{\"label\":\"AgentOS\",\"value\":\"curie-eng/agentos\"}]}}\n```",
        )
        .unwrap();
        assert_eq!(message.lines, ["Pick one"]);
        assert_eq!(message.actions[0].label, "AgentOS");
        assert_eq!(message.actions[0].value, "curie-eng/agentos");
        assert!(message.allow_free_text);
    }

    #[test]
    fn confirm_disables_free_text_by_default() {
        let message = parse_terminal_message(
            "```agentos-reply\n{\"version\":\"1.0\",\"text\":\"Deploy?\",\"interaction\":{\"kind\":\"confirm\",\"id\":\"deploy\",\"prompt\":\"Deploy?\",\"confirm\":{\"label\":\"Deploy\",\"value\":\"deploy\"},\"cancel\":{\"label\":\"Cancel\",\"value\":\"cancel\"}}}\n```",
        )
        .unwrap();
        assert!(!message.allow_free_text);
        assert_eq!(message.actions.len(), 2);
    }

    #[test]
    fn rejects_native_widgets_and_unknown_versions() {
        assert!(parse_terminal_message(
            "```agentos-reply\n{\"version\":\"1.0\",\"text\":\"x\",\"blocks\":[]}\n```"
        )
        .is_none());
        assert!(parse_terminal_message(
            "```agentos-reply\n{\"version\":\"2.0\",\"text\":\"x\"}\n```"
        )
        .is_none());
    }
}
