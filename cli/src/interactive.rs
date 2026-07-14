//! Interactive terminal interface for AgentOS.
//!
//! This is a ratatui/crossterm surface over the existing clap command grammar:
//! it does not invent a second implementation path. The TUI helps a human pick
//! a target and action, previews the exact `agentos ...` command, prompts for
//! any required values, then suspends the alternate screen and runs that command
//! as a normal child process.

use std::collections::BTreeMap;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap};
use ratatui::{Frame, Terminal};

#[derive(Clone, Debug)]
struct Recipe {
    target: &'static str,
    title: &'static str,
    description: &'static str,
    kind: RecipeKind,
    args: Vec<ArgPart>,
    fields: Vec<Field>,
    notes: &'static [&'static str],
}

#[derive(Clone, Debug)]
enum RecipeKind {
    Command,
    Workflow(Workflow),
}

#[derive(Clone, Copy, Debug)]
enum Workflow {
    McpAuthExample,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ArgPart {
    Literal(&'static str),
    Field(&'static str),
    OptionalFlag {
        flag: &'static str,
        field: &'static str,
    },
}

#[derive(Clone, Debug)]
struct Field {
    key: &'static str,
    label: &'static str,
    default: Option<&'static str>,
    required: bool,
}

#[derive(Debug)]
struct App {
    recipes: Vec<Recipe>,
    targets: Vec<&'static str>,
    target_idx: usize,
    selected: usize,
    message: String,
}

pub async fn run() -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(crate::exit::usage(
            "agentos interactive requires an interactive terminal; use the regular agentos subcommands in non-interactive sessions",
        ));
    }
    let mut app = App::new();
    let mut terminal = TerminalSession::enter()?;
    let result = event_loop(&mut terminal.terminal, &mut app);
    terminal.leave()?;
    result
}

fn event_loop(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>, app: &mut App) -> Result<()> {
    loop {
        terminal.draw(|frame| draw(frame, app))?;
        if !event::poll(Duration::from_millis(200))? {
            continue;
        }
        let Event::Key(key) = event::read()? else {
            continue;
        };
        if app.handle_key(key)? {
            break;
        }
    }
    Ok(())
}

impl App {
    fn new() -> Self {
        let recipes = recipes();
        App {
            recipes,
            targets: vec![
                "all", "skill", "examples", "secrets", "local", "cluster", "dev",
            ],
            target_idx: 0,
            selected: 0,
            message: "Select an action. Enter runs it; q exits.".into(),
        }
    }

    fn visible_indices(&self) -> Vec<usize> {
        let target = self.targets[self.target_idx];
        self.recipes
            .iter()
            .enumerate()
            .filter_map(|(idx, recipe)| (target == "all" || recipe.target == target).then_some(idx))
            .collect()
    }

    fn selected_recipe(&self) -> Option<&Recipe> {
        self.visible_indices()
            .get(self.selected)
            .and_then(|idx| self.recipes.get(*idx))
    }

    fn handle_key(&mut self, key: KeyEvent) -> Result<bool> {
        match (key.code, key.modifiers) {
            (KeyCode::Char('q'), _) | (KeyCode::Esc, _) => return Ok(true),
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(true),
            (KeyCode::Down | KeyCode::Char('j'), _) => self.move_selection(1),
            (KeyCode::Up | KeyCode::Char('k'), _) => self.move_selection(-1),
            (KeyCode::Tab | KeyCode::Right, _) => self.next_target(),
            (KeyCode::BackTab | KeyCode::Left, _) => self.prev_target(),
            (KeyCode::Enter, _) | (KeyCode::Char('r'), _) => self.run_selected()?,
            _ => {}
        }
        Ok(false)
    }

    fn move_selection(&mut self, delta: isize) {
        let len = self.visible_indices().len();
        if len == 0 {
            self.selected = 0;
            return;
        }
        self.selected = ((self.selected as isize + delta).rem_euclid(len as isize)) as usize;
    }

    fn next_target(&mut self) {
        self.target_idx = (self.target_idx + 1) % self.targets.len();
        self.selected = 0;
    }

    fn prev_target(&mut self) {
        self.target_idx = if self.target_idx == 0 {
            self.targets.len() - 1
        } else {
            self.target_idx - 1
        };
        self.selected = 0;
    }

    fn run_selected(&mut self) -> Result<()> {
        let Some(recipe) = self.selected_recipe().cloned() else {
            return Ok(());
        };
        suspend_terminal()?;
        let result = prompt_and_run(&recipe);
        resume_terminal()?;
        self.message = match result {
            Ok(()) => format!("Finished: {}", recipe.title),
            Err(err) => format!("Command failed to start or complete: {err:#}"),
        };
        Ok(())
    }
}

struct TerminalSession {
    terminal: Terminal<CrosstermBackend<io::Stdout>>,
    active: bool,
}

impl TerminalSession {
    fn enter() -> Result<Self> {
        enable_raw_mode().context("enabling terminal raw mode")?;
        execute!(io::stdout(), EnterAlternateScreen).context("entering alternate screen")?;
        let backend = CrosstermBackend::new(io::stdout());
        let mut terminal = Terminal::new(backend).context("creating terminal")?;
        terminal.clear().ok();
        Ok(TerminalSession {
            terminal,
            active: true,
        })
    }

    fn leave(&mut self) -> Result<()> {
        if self.active {
            disable_raw_mode().ok();
            execute!(io::stdout(), LeaveAlternateScreen).context("leaving alternate screen")?;
            self.active = false;
        }
        Ok(())
    }
}

impl Drop for TerminalSession {
    fn drop(&mut self) {
        let _ = self.leave();
    }
}

fn suspend_terminal() -> Result<()> {
    disable_raw_mode().ok();
    execute!(io::stdout(), LeaveAlternateScreen).context("leaving alternate screen")
}

fn resume_terminal() -> Result<()> {
    print!("Press Enter to return to AgentOS interactive...");
    io::stdout().flush().ok();
    let mut line = String::new();
    let _ = io::stdin().read_line(&mut line);
    execute!(io::stdout(), EnterAlternateScreen).context("entering alternate screen")?;
    enable_raw_mode().context("enabling terminal raw mode")
}

fn prompt_and_run(recipe: &Recipe) -> Result<()> {
    println!("AgentOS interactive: {}", recipe.title);
    println!("{}", recipe.description);
    println!();

    let mut values = BTreeMap::new();
    for field in &recipe.fields {
        let value = prompt_field(field)?;
        values.insert(field.key.to_string(), value);
    }
    match &recipe.kind {
        RecipeKind::Command => {
            let argv = build_argv(recipe, &values);
            println!();
            run_agentos(&argv, Path::new("."))?;
        }
        RecipeKind::Workflow(Workflow::McpAuthExample) => run_mcp_auth_example(&values)?,
    }
    Ok(())
}

fn run_mcp_auth_example(values: &BTreeMap<String, String>) -> Result<()> {
    let repo = values
        .get("repo")
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .unwrap_or("curie-eng/agentos");
    let message = values
        .get("message")
        .filter(|value| !value.is_empty())
        .cloned()
        .unwrap_or_else(|| format!("List the open issues in {repo} and group them by label."));
    let port = values
        .get("port")
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .unwrap_or("7247");
    let should_build = yesish(values.get("build").map(String::as_str).unwrap_or("y"));

    ensure_model_credential_available()?;
    ensure_secret_available("GITHUB_PERSONAL_ACCESS_TOKEN")?;

    let repo_root = find_repo_root(std::env::current_dir().context("reading current directory")?)
        .context(
        "could not find AgentOS repo root; run this workflow from the source checkout",
    )?;
    let example_dir = repo_root.join("examples/github-issues");
    if !example_dir.join(".mcp.json").is_file() {
        anyhow::bail!(
            "missing MCP auth example at {}; expected examples/github-issues/.mcp.json",
            example_dir.display()
        );
    }

    println!("This workflow will verify the authenticated GitHub MCP example end to end.");
    println!("Example bundle: {}", example_dir.display());
    println!("Target repository: {repo}");
    println!();

    if should_build {
        run_agentos(&["build".to_string()], &repo_root)?;
    } else {
        println!("Skipping runner image build because you answered no.");
        println!();
    }

    let container_name = "agentos-mcp-auth-example";
    let url = format!("http://localhost:{port}");
    let mut started = false;
    let run_result = (|| -> Result<()> {
        run_agentos(
            &[
                "skill".to_string(),
                "up".to_string(),
                "--plugin-dir".to_string(),
                example_dir.display().to_string(),
                "--port".to_string(),
                port.to_string(),
                "--name".to_string(),
                container_name.to_string(),
                "--secret".to_string(),
                "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
            ],
            &repo_root,
        )?;
        started = true;
        run_agentos(
            &[
                "skill".to_string(),
                "message".to_string(),
                message,
                "--url".to_string(),
                url,
            ],
            &repo_root,
        )
    })();

    if started {
        println!();
        println!("Cleaning up the example runner...");
        if let Err(err) = run_agentos(&["skill".to_string(), "down".to_string()], &example_dir) {
            println!("Cleanup warning: {err:#}");
        }
    }

    run_result?;
    println!();
    println!(
        "Pass condition: the answer above should cite live issue titles or numbers from {repo}."
    );
    Ok(())
}

fn yesish(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "" | "y" | "yes" | "true" | "1"
    )
}

fn ensure_secret_available(name: &str) -> Result<()> {
    if std::env::var_os(name).is_some() {
        println!("{name}: available from the current environment.");
        return Ok(());
    }
    if crate::secrets::has_value(name) {
        println!("{name}: available from the AgentOS secret store.");
        return Ok(());
    }
    println!("{name}: missing.");
    if prompt_yes_no(
        &format!("Save {name} in the OS credential store now?"),
        true,
    )? {
        crate::secrets::set(crate::secrets::SetSecretOpts {
            name: name.to_string(),
            from_env: None,
        })?;
        return Ok(());
    }
    anyhow::bail!("{name} is required for this workflow")
}

fn ensure_model_credential_available() -> Result<()> {
    const NAMES: &[&str] = &[
        "AGENTOS_CREDENTIALS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ];
    for name in NAMES {
        if std::env::var_os(name).is_some() {
            println!("{name}: model credential available from the current environment.");
            return Ok(());
        }
    }
    for name in NAMES {
        if crate::secrets::has_value(name) {
            println!("{name}: model credential available from the AgentOS secret store.");
            return Ok(());
        }
    }

    println!("Model credential: missing.");
    if !prompt_yes_no(
        "Save a model credential in the OS credential store now?",
        true,
    )? {
        anyhow::bail!(
            "a model credential is required; save AGENTOS_CREDENTIALS, ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN"
        );
    }
    print!("Credential name [ANTHROPIC_API_KEY]: ");
    io::stdout().flush().ok();
    let mut line = String::new();
    io::stdin()
        .read_line(&mut line)
        .context("reading credential name")?;
    let name = if line.trim().is_empty() {
        "ANTHROPIC_API_KEY"
    } else {
        line.trim()
    };
    if !NAMES.contains(&name) {
        anyhow::bail!(
            "unsupported model credential name {name}; use AGENTOS_CREDENTIALS, ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN"
        );
    }
    crate::secrets::set(crate::secrets::SetSecretOpts {
        name: name.to_string(),
        from_env: None,
    })?;
    Ok(())
}

fn prompt_yes_no(prompt: &str, default: bool) -> Result<bool> {
    loop {
        let suffix = if default { "[Y/n]" } else { "[y/N]" };
        print!("{prompt} {suffix}: ");
        io::stdout().flush().ok();
        let mut line = String::new();
        io::stdin().read_line(&mut line)?;
        let value = line.trim().to_ascii_lowercase();
        if value.is_empty() {
            return Ok(default);
        }
        match value.as_str() {
            "y" | "yes" => return Ok(true),
            "n" | "no" => return Ok(false),
            _ => println!("Please answer y or n."),
        }
    }
}

fn secret_status(name: &str) -> &'static str {
    if std::env::var_os(name).is_some() {
        "env"
    } else if crate::secrets::has_value(name) {
        "saved"
    } else {
        "missing"
    }
}

fn model_credential_status() -> &'static str {
    for name in [
        "AGENTOS_CREDENTIALS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ] {
        if std::env::var_os(name).is_some() || crate::secrets::has_value(name) {
            return "available";
        }
    }
    "missing"
}

fn secrets_status_lines() -> Vec<Line<'static>> {
    vec![
        Line::from(format!("Model credential: {}", model_credential_status())),
        Line::from(format!(
            "GITHUB_PERSONAL_ACCESS_TOKEN: {}",
            secret_status("GITHUB_PERSONAL_ACCESS_TOKEN")
        )),
    ]
}

fn maybe_add_secret_status(lines: &mut Vec<Line<'static>>, workflow: Workflow) {
    match workflow {
        Workflow::McpAuthExample => {
            lines.push(Line::from(Span::styled(
                "Credential status",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.extend(secrets_status_lines());
            lines.push(Line::from(""));
        }
    }
}

fn find_repo_root(mut dir: PathBuf) -> Option<PathBuf> {
    loop {
        if dir.join("runner/Dockerfile").is_file() && dir.join("examples/github-issues").is_dir() {
            return Some(dir);
        }
        if !dir.pop() {
            return None;
        }
    }
}

fn run_agentos(argv: &[String], cwd: &Path) -> Result<()> {
    println!("$ {}", render_command(argv));
    println!();
    let status = Command::new(std::env::current_exe().context("resolving current executable")?)
        .args(argv)
        .current_dir(cwd)
        .status()
        .with_context(|| format!("running {}", render_command(argv)))?;
    if !status.success() {
        anyhow::bail!("{} exited with {status}", render_command(argv));
    }
    Ok(())
}

fn prompt_field(field: &Field) -> Result<String> {
    loop {
        match field.default {
            Some(default) => print!("{} [{default}]: ", field.label),
            None => print!("{}: ", field.label),
        }
        io::stdout().flush().ok();
        let mut line = String::new();
        io::stdin()
            .read_line(&mut line)
            .context("reading interactive answer")?;
        let trimmed = line.trim();
        let value = if trimmed.is_empty() {
            field.default.unwrap_or("").to_string()
        } else {
            trimmed.to_string()
        };
        if !field.required || !value.is_empty() {
            return Ok(value);
        }
        println!("{} is required.", field.label);
    }
}

fn build_argv(recipe: &Recipe, values: &BTreeMap<String, String>) -> Vec<String> {
    let mut argv = Vec::new();
    for part in &recipe.args {
        match part {
            ArgPart::Literal(value) => argv.push((*value).to_string()),
            ArgPart::Field(field) => {
                if let Some(value) = values.get(*field) {
                    if !value.is_empty() {
                        argv.push(value.clone());
                    }
                }
            }
            ArgPart::OptionalFlag { flag, field } => {
                if let Some(value) = values.get(*field) {
                    if !value.is_empty() {
                        argv.push((*flag).to_string());
                        argv.push(value.clone());
                    }
                }
            }
        }
    }
    argv
}

fn render_command(argv: &[String]) -> String {
    std::iter::once("agentos".to_string())
        .chain(argv.iter().map(|arg| shell_quote(arg)))
        .collect::<Vec<_>>()
        .join(" ")
}

fn shell_quote(arg: &str) -> String {
    if arg
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "-_./:=@".contains(c))
    {
        arg.to_string()
    } else {
        format!("'{}'", arg.replace('\'', "'\\''"))
    }
}

fn draw(frame: &mut Frame<'_>, app: &App) {
    let area = frame.area();
    let layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(12),
            Constraint::Length(3),
        ])
        .split(area);

    draw_header(frame, layout[0], app);
    draw_body(frame, layout[1], app);
    draw_footer(frame, layout[2], app);
}

fn draw_header(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let title = Line::from(vec![
        Span::styled("AgentOS", Style::default().fg(Color::Cyan).bold()),
        Span::raw(" interactive  "),
        Span::styled(
            format!("target: {}", app.targets[app.target_idx]),
            Style::default().fg(Color::Yellow),
        ),
    ]);
    frame.render_widget(
        Paragraph::new(title)
            .alignment(Alignment::Center)
            .block(Block::default().borders(Borders::ALL)),
        area,
    );
}

fn draw_body(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(18),
            Constraint::Percentage(36),
            Constraint::Percentage(64),
        ])
        .split(area);
    draw_targets(frame, chunks[0], app);
    draw_actions(frame, chunks[1], app);
    draw_detail(frame, chunks[2], app);
}

fn draw_targets(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let items: Vec<ListItem<'_>> = app
        .targets
        .iter()
        .map(|target| ListItem::new(Line::from(*target)))
        .collect();
    let mut state = ListState::default();
    state.select(Some(app.target_idx));
    frame.render_stateful_widget(
        List::new(items)
            .block(Block::default().title("Targets").borders(Borders::ALL))
            .highlight_style(Style::default().fg(Color::Black).bg(Color::Cyan)),
        area,
        &mut state,
    );
}

fn draw_actions(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let visible = app.visible_indices();
    let items: Vec<ListItem<'_>> = visible
        .iter()
        .filter_map(|idx| app.recipes.get(*idx))
        .map(|recipe| {
            ListItem::new(vec![
                Line::from(Span::styled(recipe.title, Style::default().bold())),
                Line::from(Span::styled(
                    recipe.description,
                    Style::default().fg(Color::Gray),
                )),
            ])
        })
        .collect();
    let mut state = ListState::default();
    if !items.is_empty() {
        state.select(Some(app.selected.min(items.len() - 1)));
    }
    frame.render_stateful_widget(
        List::new(items)
            .block(Block::default().title("Actions").borders(Borders::ALL))
            .highlight_style(Style::default().fg(Color::Black).bg(Color::Green)),
        area,
        &mut state,
    );
}

fn draw_detail(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let Some(recipe) = app.selected_recipe() else {
        frame.render_widget(Clear, area);
        return;
    };
    let values: BTreeMap<String, String> = recipe
        .fields
        .iter()
        .filter_map(|field| {
            field
                .default
                .map(|value| (field.key.to_string(), value.to_string()))
        })
        .collect();
    let mut lines = vec![
        Line::from(Span::styled(recipe.title, Style::default().bold())),
        Line::from(""),
        Line::from(recipe.description),
        Line::from(""),
    ];
    match &recipe.kind {
        RecipeKind::Command => {
            let preview = build_argv(recipe, &values);
            lines.push(Line::from(Span::styled(
                "Command preview",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.push(Line::from(render_command(&preview)));
            lines.push(Line::from(""));
        }
        RecipeKind::Workflow(Workflow::McpAuthExample) => {
            lines.push(Line::from(Span::styled(
                "Guided workflow",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.push(Line::from("1. Check model and GitHub token env vars"));
            lines.push(Line::from("2. Build the runner image if requested"));
            lines.push(Line::from(
                "3. Start examples/github-issues with --secret GITHUB_PERSONAL_ACCESS_TOKEN",
            ));
            lines.push(Line::from("4. Send a live GitHub issue query"));
            lines.push(Line::from("5. Stop the example runner"));
            lines.push(Line::from(""));
            maybe_add_secret_status(&mut lines, Workflow::McpAuthExample);
        }
    }
    if !recipe.fields.is_empty() {
        lines.push(Line::from(Span::styled(
            "Prompts before run",
            Style::default().fg(Color::Yellow).bold(),
        )));
        for field in &recipe.fields {
            let default = field.default.unwrap_or("required");
            lines.push(Line::from(format!("{}: {default}", field.label)));
        }
        lines.push(Line::from(""));
    }
    if !recipe.notes.is_empty() {
        lines.push(Line::from(Span::styled(
            "Notes",
            Style::default().fg(Color::Yellow).bold(),
        )));
        for note in recipe.notes {
            lines.push(Line::from(format!("- {note}")));
        }
    }
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .wrap(Wrap { trim: false })
            .block(Block::default().title("Details").borders(Borders::ALL)),
        area,
    );
}

fn draw_footer(frame: &mut Frame<'_>, area: Rect, app: &App) {
    let text = Line::from(vec![
        Span::styled("Up/Down", Style::default().bold()),
        Span::raw(" action  "),
        Span::styled("Tab/Left/Right", Style::default().bold()),
        Span::raw(" target  "),
        Span::styled("Enter/r", Style::default().bold()),
        Span::raw(" run  "),
        Span::styled("q/Esc", Style::default().bold()),
        Span::raw(" quit    "),
        Span::styled(&app.message, Style::default().fg(Color::Gray)),
    ]);
    frame.render_widget(
        Paragraph::new(text)
            .alignment(Alignment::Center)
            .block(Block::default().borders(Borders::ALL)),
        area,
    );
}

fn recipes() -> Vec<Recipe> {
    vec![
        Recipe {
            target: "skill",
            title: "Start runner",
            description: "Boot the current plugin bundle in a local runner container.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("up"),
                ArgPart::OptionalFlag {
                    flag: "--plugin-dir",
                    field: "plugin_dir",
                },
                ArgPart::OptionalFlag {
                    flag: "--model",
                    field: "model",
                },
            ],
            fields: vec![
                Field {
                    key: "plugin_dir",
                    label: "Plugin directory",
                    default: Some("."),
                    required: false,
                },
                Field {
                    key: "model",
                    label: "Model id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &[
                "Use --fake-model or --local-model from the regular CLI when you need those modes.",
            ],
        },
        Recipe {
            target: "skill",
            title: "Send skill message",
            description: "Send a synthetic event to the local runner and stream the reply.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
            ],
            fields: vec![Field {
                key: "text",
                label: "Message text",
                default: None,
                required: true,
            }],
            notes: &["Requires a running `agentos skill up` session."],
        },
        Recipe {
            target: "skill",
            title: "Run skill eval",
            description: "Run evals/cases.json through the local runner.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("eval"),
                ArgPart::OptionalFlag {
                    flag: "--cases",
                    field: "cases",
                },
            ],
            fields: vec![Field {
                key: "cases",
                label: "Cases file",
                default: Some("evals/cases.json"),
                required: false,
            }],
            notes: &[],
        },
        Recipe {
            target: "examples",
            title: "Verify MCP auth example",
            description: "Guided e2e for examples/github-issues using a live GitHub MCP server.",
            kind: RecipeKind::Workflow(Workflow::McpAuthExample),
            args: vec![],
            fields: vec![
                Field {
                    key: "repo",
                    label: "GitHub repo to inspect",
                    default: Some("curie-eng/agentos"),
                    required: true,
                },
                Field {
                    key: "message",
                    label: "Live verification prompt",
                    default: Some(
                        "List the open issues in curie-eng/agentos and group them by label.",
                    ),
                    required: true,
                },
                Field {
                    key: "port",
                    label: "Runner host port",
                    default: Some("7247"),
                    required: true,
                },
                Field {
                    key: "build",
                    label: "Build runner image first? (y/n)",
                    default: Some("y"),
                    required: true,
                },
            ],
            notes: &[
                "Requires a model credential in the shell: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or AGENTOS_CREDENTIALS.",
                "Requires GITHUB_PERSONAL_ACCESS_TOKEN in the shell; the value is forwarded by name and not placed in argv.",
                "A passing run cites live GitHub issue titles or numbers.",
            ],
        },
        Recipe {
            target: "secrets",
            title: "Save secret",
            description: "Store a local secret in the OS credential store with hidden input.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("secrets"),
                ArgPart::Literal("set"),
                ArgPart::Field("name"),
            ],
            fields: vec![Field {
                key: "name",
                label: "Secret name",
                default: Some("GITHUB_PERSONAL_ACCESS_TOKEN"),
                required: true,
            }],
            notes: &[
                "The value is prompted with hidden input and saved in the OS credential store.",
                "Use env-style names such as ANTHROPIC_API_KEY or GITHUB_PERSONAL_ACCESS_TOKEN.",
            ],
        },
        Recipe {
            target: "secrets",
            title: "List saved secrets",
            description: "List saved AgentOS secret names without printing values.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("secrets"), ArgPart::Literal("list")],
            fields: vec![],
            notes: &["Only names are listed; secret values stay in the OS credential store."],
        },
        Recipe {
            target: "secrets",
            title: "Remove secret",
            description: "Remove a saved secret from the OS credential store.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("secrets"),
                ArgPart::Literal("unset"),
                ArgPart::Field("name"),
            ],
            fields: vec![Field {
                key: "name",
                label: "Secret name",
                default: Some("GITHUB_PERSONAL_ACCESS_TOKEN"),
                required: true,
            }],
            notes: &[],
        },
        Recipe {
            target: "local",
            title: "Start local stack",
            description: "Bring up the compose stack for the local platform loop.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("local"), ArgPart::Literal("up")],
            fields: vec![],
            notes: &["Use the regular CLI for --minimal, --slack, or --local-model variants."],
        },
        Recipe {
            target: "local",
            title: "Send local message",
            description: "Drive the compose stack end to end with zero Slack contact.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("local"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![
                Field {
                    key: "text",
                    label: "Message text",
                    default: None,
                    required: true,
                },
                Field {
                    key: "channel",
                    label: "Slack channel id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &["Requires `agentos local up` and a deployed local agent."],
        },
        Recipe {
            target: "local",
            title: "Local status",
            description: "Show compose service status.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("local"), ArgPart::Literal("status")],
            fields: vec![],
            notes: &[],
        },
        Recipe {
            target: "cluster",
            title: "Cluster status",
            description: "Report release health and access URLs.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("cluster"),
                ArgPart::Literal("status"),
                ArgPart::OptionalFlag {
                    flag: "--namespace",
                    field: "namespace",
                },
                ArgPart::OptionalFlag {
                    flag: "--release",
                    field: "release",
                },
            ],
            fields: vec![
                Field {
                    key: "namespace",
                    label: "Namespace",
                    default: Some("agentos"),
                    required: false,
                },
                Field {
                    key: "release",
                    label: "Release",
                    default: Some("agentos"),
                    required: false,
                },
            ],
            notes: &[],
        },
        Recipe {
            target: "cluster",
            title: "Send cluster message",
            description: "Drive a deployed release end to end with zero Slack contact.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("cluster"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![
                Field {
                    key: "text",
                    label: "Message text",
                    default: None,
                    required: true,
                },
                Field {
                    key: "channel",
                    label: "Slack channel id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &["Requires an installed release and a deployed agent."],
        },
        Recipe {
            target: "dev",
            title: "Install checkout",
            description: "Bootstrap a dev checkout: deps, CLI build, runner image.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("install")],
            fields: vec![],
            notes: &["Starts nothing; run once after cloning."],
        },
        Recipe {
            target: "dev",
            title: "Check contracts",
            description: "Run the frozen contract drift checks.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("dev"), ArgPart::Literal("contracts")],
            fields: vec![],
            notes: &[],
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_argv_omits_empty_optional_flags() {
        let recipe = Recipe {
            target: "skill",
            title: "x",
            description: "x",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![],
            notes: &[],
        };
        let mut values = BTreeMap::new();
        values.insert("text".to_string(), "hello world".to_string());
        values.insert("channel".to_string(), String::new());
        assert_eq!(
            build_argv(&recipe, &values),
            vec!["skill", "message", "hello world"]
        );
    }

    #[test]
    fn render_command_quotes_shell_specials() {
        let argv = vec!["skill".into(), "message".into(), "hello world".into()];
        assert_eq!(render_command(&argv), "agentos skill message 'hello world'");
    }

    #[test]
    fn examples_target_includes_mcp_auth_workflow() {
        let app = App::new();
        let idx = app
            .targets
            .iter()
            .position(|target| *target == "examples")
            .expect("examples target exists");
        let mut app = app;
        app.target_idx = idx;
        let titles: Vec<&str> = app
            .visible_indices()
            .iter()
            .map(|idx| app.recipes[*idx].title)
            .collect();
        assert!(titles.contains(&"Verify MCP auth example"));
    }
}
