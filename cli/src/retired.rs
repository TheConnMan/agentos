//! Retired command hints for the noun based CLI surface.

/// Return a one line hint for retired command forms.
pub fn retired_hint(args: &[String]) -> Option<String> {
    if args.iter().any(|arg| arg == "--local") {
        return Some(
            "`--local` was retired in favor of the local target. Use `agentos local message` for compose stack turns and `agentos local deploy` for local API deploys.".to_string(),
        );
    }

    // Keep this global-flag skip list in sync with the `global = true` flags on
    // the `Cli` struct in main.rs (debug, quiet, color). A new global flag added
    // there must be added here too, or retired_hint will misread it as the
    // subcommand token.
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--debug" | "-q" | "--quiet" => {
                index += 1;
            }
            "--color" => {
                index += 1;
                if index < args.len() {
                    index += 1;
                }
            }
            token if token.starts_with("--color=") => {
                index += 1;
            }
            _ => break,
        }
    }

    let token = args.get(index)?.as_str();
    match token {
        "init" | "skill" | "local" | "cluster" | "-h" | "--help" | "-V" | "--version" => None,
        "start" => Some("`agentos start` was retired. Use `agentos skill up`.".to_string()),
        "stop" => Some("`agentos stop` was retired. Use `agentos skill down`.".to_string()),
        "send" => {
            Some("`agentos send` was retired. Use `agentos skill message`.".to_string())
        }
        "runner-status" => Some(
            "`agentos runner-status` was retired. Use `agentos skill status`.".to_string(),
        ),
        "eval" => Some("`agentos eval` was retired. Use `agentos skill eval`.".to_string()),
        "chat" => {
            Some("`agentos chat` was retired. Use `agentos local message`.".to_string())
        }
        "up" => Some("`agentos up` was retired. Use `agentos cluster up`.".to_string()),
        "down" => {
            Some("`agentos down` was retired. Use `agentos cluster down`.".to_string())
        }
        "status" => Some(
            "`agentos status` was retired. Use `agentos cluster status`.".to_string(),
        ),
        "message" => Some(
            "`agentos message` was retired. Use `agentos cluster message`.".to_string(),
        ),
        "deploy" => Some(
            "`agentos deploy` was retired. Use `agentos cluster deploy`.".to_string(),
        ),
        "steer" => Some(
            "`agentos steer` was removed. Start a new turn with `agentos skill message` instead.".to_string(),
        ),
        "interrupt" => Some(
            "`agentos interrupt` was removed. Use Ctrl-C to stop the current CLI process, then restart with `agentos skill message` if you need a new turn.".to_string(),
        ),
        _ => None,
    }
}
