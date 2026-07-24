//! Retired command hints for the noun based CLI surface.

/// Return a one line hint for retired command forms.
pub fn retired_hint(args: &[String]) -> Option<String> {
    if args.iter().any(|arg| arg == "--local") {
        return Some(
            "`--local` was retired in favor of the local target. Use `curie local message` for compose stack turns and `curie local deploy` for local API deploys.".to_string(),
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
        "start" => Some("`curie start` was retired. Use `curie skill up`.".to_string()),
        "stop" => Some("`curie stop` was retired. Use `curie skill down`.".to_string()),
        "send" => {
            Some("`curie send` was retired. Use `curie skill message`.".to_string())
        }
        "runner-status" => Some(
            "`curie runner-status` was retired. Use `curie skill status`.".to_string(),
        ),
        "eval" => Some("`curie eval` was retired. Use `curie skill eval`.".to_string()),
        "chat" => {
            Some("`curie chat` was retired. Use `curie local message`.".to_string())
        }
        "up" => Some("`curie up` was retired. Use `curie cluster up`.".to_string()),
        "down" => {
            Some("`curie down` was retired. Use `curie cluster down`.".to_string())
        }
        "status" => Some(
            "`curie status` was retired. Use `curie cluster status`.".to_string(),
        ),
        "message" => Some(
            "`curie message` was retired. Use `curie cluster message`.".to_string(),
        ),
        "deploy" => Some(
            "`curie deploy` was retired. Use `curie cluster deploy`.".to_string(),
        ),
        "steer" => Some(
            "`curie steer` was removed. Start a new turn with `curie skill message` instead.".to_string(),
        ),
        "interrupt" => Some(
            "`curie interrupt` was removed. Use Ctrl-C to stop the current CLI process, then restart with `curie skill message` if you need a new turn.".to_string(),
        ),
        _ => None,
    }
}
