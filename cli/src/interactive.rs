//! Interactive terminal interface for AgentOS.
//!
//! This is a ratatui/crossterm surface over the existing clap command grammar:
//! it does not invent a second implementation path. The TUI helps a human pick
//! a target and action, previews the exact `agentos ...` command, prompts for
//! any required values, then keeps prompts, command output, and workflow results
//! inside the alternate-screen interface.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, BufReader, IsTerminal};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
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
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

#[derive(Clone, Debug, PartialEq, Eq)]
enum SecretNameChoice {
    Name(String),
    Custom,
}

#[derive(Clone, Debug)]
struct SelectChoice<T> {
    label: String,
    description: String,
    value: T,
}

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
    Tui(TuiAction),
    Workflow(Workflow),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TuiAction {
    SaveSecret,
    ListSecrets,
    RemoveSecret,
}

#[derive(Clone, Copy, Debug)]
enum Workflow {
    GithubAgentChat,
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
        if app.handle_key(key, terminal)? {
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
            targets: vec!["all", "skill", "secrets", "local", "cluster", "dev"],
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

    fn handle_key(
        &mut self,
        key: KeyEvent,
        terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    ) -> Result<bool> {
        match (key.code, key.modifiers) {
            (KeyCode::Char('q'), _) | (KeyCode::Esc, _) => return Ok(true),
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(true),
            (KeyCode::Down | KeyCode::Char('j'), _) => self.move_selection(1),
            (KeyCode::Up | KeyCode::Char('k'), _) => self.move_selection(-1),
            (KeyCode::Tab | KeyCode::Right, _) => self.next_target(),
            (KeyCode::BackTab | KeyCode::Left, _) => self.prev_target(),
            (KeyCode::Enter, _) | (KeyCode::Char('r'), _) => self.run_selected(terminal)?,
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

    fn run_selected(
        &mut self,
        terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    ) -> Result<()> {
        let Some(recipe) = self.selected_recipe().cloned() else {
            return Ok(());
        };
        let result = match &recipe.kind {
            RecipeKind::Tui(action) => run_tui_action(*action, terminal, self),
            RecipeKind::Command | RecipeKind::Workflow(_) => {
                run_recipe_in_tui(terminal, self, &recipe)
            }
        };
        self.message = match result {
            Ok(message) => message,
            Err(err) => format!("Action failed: {err:#}"),
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

fn prompt_recipe_fields(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    recipe: &Recipe,
) -> Result<Option<BTreeMap<String, String>>> {
    let mut values = BTreeMap::new();
    for (idx, field) in recipe.fields.iter().enumerate() {
        let title = format!(
            "{} · Step {} of {}",
            recipe.title,
            idx + 1,
            recipe.fields.len()
        );
        let Some(value) = prompt_text(
            terminal,
            app,
            &title,
            field.label,
            field.default,
            false,
            !field.required,
        )?
        else {
            return Ok(None);
        };
        if field.required && value.is_empty() {
            return Ok(None);
        }
        values.insert(field.key.to_string(), value);
    }
    Ok(Some(values))
}

fn run_recipe_in_tui(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    recipe: &Recipe,
) -> Result<String> {
    let Some(values) = prompt_recipe_fields(terminal, app, recipe)? else {
        return Ok(format!("Canceled: {}", recipe.title));
    };
    if matches!(recipe.kind, RecipeKind::Workflow(Workflow::GithubAgentChat)) {
        run_github_agent_chat(terminal, app, &values)?;
        return Ok(format!("Finished: {}", recipe.title));
    }
    let mut view = RunView::new(recipe.title);
    let run_result = match &recipe.kind {
        RecipeKind::Command => {
            let argv = build_argv(recipe, &values);
            run_agentos_in_tui(terminal, app, &argv, Path::new("."), &mut view)
        }
        RecipeKind::Tui(_) => Ok(()),
        RecipeKind::Workflow(_) => unreachable!("workflows run before the command view"),
    };
    if let Err(err) = &run_result {
        view.push("");
        view.push(format!("ERROR: {err:#}"));
    }
    show_run_view(terminal, app, &mut view)?;
    run_result?;
    Ok(format!("Finished: {}", recipe.title))
}

fn run_tui_action(
    action: TuiAction,
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
) -> Result<String> {
    match action {
        TuiAction::SaveSecret => {
            let Some(choice) = prompt_select(
                terminal,
                app,
                "Save Secret",
                "Choose a secret name",
                secret_name_choices()?,
            )?
            else {
                return Ok("Save secret canceled.".to_string());
            };
            let name = match choice {
                SecretNameChoice::Name(name) => name,
                SecretNameChoice::Custom => {
                    let Some(name) = prompt_text(
                        terminal,
                        app,
                        "Save Secret",
                        "Custom secret name",
                        None,
                        false,
                        false,
                    )?
                    else {
                        return Ok("Save secret canceled.".to_string());
                    };
                    name
                }
            };
            crate::secrets::validate_name(&name)?;
            let Some(value) = prompt_text(terminal, app, "Save Secret", &name, None, true, false)?
            else {
                return Ok("Save secret canceled.".to_string());
            };
            crate::secrets::save_value(&name, &value)?;
            Ok(format!("Saved {name} in the OS credential store."))
        }
        TuiAction::ListSecrets => {
            let count = crate::secrets::list_names()?.len();
            Ok(match count {
                0 => "No AgentOS secrets saved.".to_string(),
                1 => "Showing 1 saved secret name.".to_string(),
                n => format!("Showing {n} saved secret names."),
            })
        }
        TuiAction::RemoveSecret => {
            let Some(choice) = prompt_select(
                terminal,
                app,
                "Remove Secret",
                "Choose a saved secret",
                saved_secret_choices()?,
            )?
            else {
                return Ok("Remove secret canceled.".to_string());
            };
            let name = match choice {
                SecretNameChoice::Name(name) => name,
                SecretNameChoice::Custom => {
                    let Some(name) = prompt_text(
                        terminal,
                        app,
                        "Remove Secret",
                        "Custom secret name",
                        None,
                        false,
                        false,
                    )?
                    else {
                        return Ok("Remove secret canceled.".to_string());
                    };
                    name
                }
            };
            crate::secrets::validate_name(&name)?;
            let Some(confirm) = prompt_text(
                terminal,
                app,
                "Remove Secret",
                &format!("Type {name} to confirm"),
                None,
                false,
                false,
            )?
            else {
                return Ok("Remove secret canceled.".to_string());
            };
            if confirm != name {
                return Ok("Remove secret canceled.".to_string());
            }
            crate::secrets::remove_value(&name)?;
            Ok(format!("Removed {name}."))
        }
    }
}

fn secret_name_choices() -> Result<Vec<SelectChoice<SecretNameChoice>>> {
    let saved_names = crate::secrets::list_names()?.into_iter().collect();
    Ok(secret_name_choices_for(&saved_names))
}

fn secret_name_choices_for(saved_names: &BTreeSet<String>) -> Vec<SelectChoice<SecretNameChoice>> {
    let common = [
        ("ANTHROPIC_API_KEY", "Anthropic API key for model calls"),
        (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "Claude Code OAuth token for model calls",
        ),
        (
            "AGENTOS_CREDENTIALS",
            "Provider credential forwarded as AgentOS credentials",
        ),
        (
            "OPENAI_API_KEY",
            "OpenAI integrations (not AgentOS runner model auth)",
        ),
        (
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "GitHub token for MCP examples or bundles",
        ),
    ];
    let mut choices = common
        .into_iter()
        .map(|(name, description)| {
            let saved = saved_names.contains(name);
            SelectChoice {
                label: if saved {
                    format!("{name} ✓")
                } else {
                    name.to_string()
                },
                description: if saved {
                    format!("{description} (saved)")
                } else {
                    description.to_string()
                },
                value: SecretNameChoice::Name(name.to_string()),
            }
        })
        .collect::<Vec<_>>();
    choices.push(SelectChoice {
        label: "Custom secret name".to_string(),
        description: "Enter any env-style secret name".to_string(),
        value: SecretNameChoice::Custom,
    });
    choices
}

fn saved_secret_choices() -> Result<Vec<SelectChoice<SecretNameChoice>>> {
    let mut choices: Vec<SelectChoice<SecretNameChoice>> = crate::secrets::list_names()?
        .into_iter()
        .map(|name| SelectChoice {
            label: name.clone(),
            description: "Saved in the OS credential store".to_string(),
            value: SecretNameChoice::Name(name),
        })
        .collect();
    choices.push(SelectChoice {
        label: "Custom secret name".to_string(),
        description: "Remove a name not shown here".to_string(),
        value: SecretNameChoice::Custom,
    });
    Ok(choices)
}

fn prompt_select<T: Clone>(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    title: &str,
    label: &str,
    choices: Vec<SelectChoice<T>>,
) -> Result<Option<T>> {
    if choices.is_empty() {
        return Ok(None);
    }
    let mut selected = 0usize;
    loop {
        terminal.draw(|frame| {
            draw(frame, app);
            draw_select_prompt(frame, title, label, &choices, selected);
        })?;
        let Event::Key(key) = event::read()? else {
            continue;
        };
        match (key.code, key.modifiers) {
            (KeyCode::Esc, _) => return Ok(None),
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(None),
            (KeyCode::Enter, _) => return Ok(Some(choices[selected].value.clone())),
            (KeyCode::Down | KeyCode::Char('j'), _) => {
                selected = (selected + 1) % choices.len();
            }
            (KeyCode::Up | KeyCode::Char('k'), _) => {
                selected = if selected == 0 {
                    choices.len() - 1
                } else {
                    selected - 1
                };
            }
            (KeyCode::Char(ch), _) if ch.is_ascii_digit() => {
                if let Some(digit) = ch.to_digit(10) {
                    let idx = digit as usize;
                    if idx > 0 && idx <= choices.len() {
                        selected = idx - 1;
                    }
                }
            }
            _ => {}
        }
    }
}

fn prompt_text(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    title: &str,
    label: &str,
    default: Option<&str>,
    secret: bool,
    allow_empty: bool,
) -> Result<Option<String>> {
    let mut value = String::new();
    loop {
        terminal.draw(|frame| {
            draw(frame, app);
            draw_prompt(frame, title, label, default, &value, secret, allow_empty);
        })?;
        let Event::Key(key) = event::read()? else {
            continue;
        };
        match (key.code, key.modifiers) {
            (KeyCode::Esc, _) => return Ok(None),
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(None),
            (KeyCode::Enter, _) => {
                if value.is_empty() {
                    if let Some(default) = default {
                        return Ok(Some(default.to_string()));
                    }
                    if allow_empty {
                        return Ok(Some(String::new()));
                    }
                    continue;
                }
                return Ok(Some(value));
            }
            (KeyCode::Backspace, _) => {
                value.pop();
            }
            (KeyCode::Char(ch), _) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                value.push(ch);
            }
            _ => {}
        }
    }
}

fn run_github_agent_chat(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    _values: &BTreeMap<String, String>,
) -> Result<()> {
    ensure_model_credential_available(terminal, app)?;
    ensure_secret_available(terminal, app, "GITHUB_PERSONAL_ACCESS_TOKEN")?;

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

    let container_name = "agentos-github-agent-chat";
    let port = "7247";
    let url = format!("http://localhost:{port}");
    let mut setup = RunView::new("Starting GitHub agent");
    let mut started = false;
    let run_result = (|| -> Result<()> {
        run_agentos_in_tui(
            terminal,
            app,
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
            &mut setup,
        )?;
        started = true;
        chat_with_runner(terminal, &repo_root, &url)
    })();

    if started {
        let mut cleanup = RunView::new("Stopping GitHub agent");
        if let Err(err) = run_agentos_in_tui(
            terminal,
            app,
            &["skill".to_string(), "down".to_string()],
            &example_dir,
            &mut cleanup,
        ) {
            cleanup.push(format!("Cleanup warning: {err:#}"));
            show_run_view(terminal, app, &mut cleanup)?;
        }
    }

    if let Err(err) = run_result {
        setup.push("");
        setup.push(format!("ERROR: {err:#}"));
        show_run_view(terminal, app, &mut setup)?;
        return Err(err);
    }
    Ok(())
}

fn ensure_secret_available(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    name: &str,
) -> Result<()> {
    if std::env::var_os(name).is_some() {
        return Ok(());
    }
    if crate::secrets::is_saved(name)? {
        return Ok(());
    }
    let Some(save) = prompt_select(
        terminal,
        app,
        "Missing Credential",
        &format!("{name} is required"),
        vec![
            SelectChoice {
                label: "Save it now".to_string(),
                description: "Store it in the OS credential store".to_string(),
                value: true,
            },
            SelectChoice {
                label: "Cancel workflow".to_string(),
                description: "Return to AgentOS without running".to_string(),
                value: false,
            },
        ],
    )?
    else {
        anyhow::bail!("{name} is required for this workflow");
    };
    if !save {
        anyhow::bail!("{name} is required for this workflow");
    }
    let Some(value) = prompt_text(terminal, app, "Save Secret", name, None, true, false)? else {
        anyhow::bail!("{name} is required for this workflow");
    };
    crate::secrets::save_value(name, &value)
}

fn ensure_model_credential_available(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
) -> Result<()> {
    const NAMES: &[&str] = &[
        "AGENTOS_CREDENTIALS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ];
    for name in NAMES {
        if std::env::var_os(name).is_some() {
            return Ok(());
        }
    }
    let saved_names = crate::secrets::list_names()?
        .into_iter()
        .collect::<BTreeSet<_>>();
    if NAMES.iter().any(|name| saved_names.contains(*name)) {
        return Ok(());
    }

    let choices = NAMES
        .iter()
        .map(|name| SelectChoice {
            label: (*name).to_string(),
            description: "Supported by the AgentOS Claude SDK runner".to_string(),
            value: (*name).to_string(),
        })
        .collect();
    let Some(name) = prompt_select(
        terminal,
        app,
        "Missing Model Credential",
        "Choose a model credential to save",
        choices,
    )?
    else {
        anyhow::bail!("a supported model credential is required");
    };
    let Some(value) = prompt_text(
        terminal,
        app,
        "Save Model Credential",
        &name,
        None,
        true,
        false,
    )?
    else {
        anyhow::bail!("a supported model credential is required");
    };
    crate::secrets::save_value(&name, &value)
}

fn secret_status(name: &str, saved_names: &BTreeSet<String>) -> &'static str {
    if std::env::var_os(name).is_some() {
        "env"
    } else if saved_names.contains(name) {
        "saved"
    } else {
        "missing"
    }
}

fn model_credential_status(saved_names: &BTreeSet<String>) -> &'static str {
    for name in [
        "AGENTOS_CREDENTIALS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ] {
        if std::env::var_os(name).is_some() || saved_names.contains(name) {
            return "available";
        }
    }
    "missing"
}

fn secrets_status_lines() -> Vec<Line<'static>> {
    let saved_names = match crate::secrets::list_names() {
        Ok(names) => names.into_iter().collect::<BTreeSet<_>>(),
        Err(err) => {
            return vec![Line::from(format!(
                "Unable to read saved credential names: {err:#}"
            ))];
        }
    };
    vec![
        Line::from(format!(
            "Model credential: {}",
            model_credential_status(&saved_names)
        )),
        Line::from(format!(
            "GITHUB_PERSONAL_ACCESS_TOKEN: {}",
            secret_status("GITHUB_PERSONAL_ACCESS_TOKEN", &saved_names)
        )),
    ]
}

fn maybe_add_secret_status(lines: &mut Vec<Line<'static>>, workflow: Workflow) {
    match workflow {
        Workflow::GithubAgentChat => {
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

#[derive(Debug)]
struct RunView {
    title: String,
    lines: Vec<String>,
    scroll: u16,
    follow: bool,
    running: bool,
}

impl RunView {
    fn new(title: &str) -> Self {
        Self {
            title: title.to_string(),
            lines: Vec::new(),
            scroll: 0,
            follow: true,
            running: true,
        }
    }

    fn push(&mut self, line: impl Into<String>) {
        self.lines.push(line.into());
    }

    fn follow_tail(&mut self, terminal_width: u16, terminal_height: u16) {
        if self.follow {
            self.scroll = self.max_scroll(terminal_width, terminal_height);
        }
    }

    fn max_scroll(&self, terminal_width: u16, terminal_height: u16) -> u16 {
        let (output_width, viewport) = run_view_dimensions(terminal_width, terminal_height);
        wrap_output_lines(&self.lines, output_width)
            .len()
            .saturating_sub(viewport)
            .min(u16::MAX as usize) as u16
    }

    fn scroll_up(&mut self, amount: u16, terminal_width: u16, terminal_height: u16) {
        if self.follow {
            self.scroll = self.max_scroll(terminal_width, terminal_height);
        }
        self.follow = false;
        self.scroll = self.scroll.saturating_sub(amount);
    }

    fn scroll_down(&mut self, amount: u16, terminal_width: u16, terminal_height: u16) {
        self.follow = false;
        self.scroll = self
            .scroll
            .saturating_add(amount)
            .min(self.max_scroll(terminal_width, terminal_height));
    }
}

#[derive(Debug)]
struct ChatView {
    lines: Vec<String>,
    input: String,
    scroll: u16,
    follow: bool,
    thinking: bool,
}

impl ChatView {
    fn new() -> Self {
        Self {
            lines: vec![
                "GitHub agent is ready.".to_string(),
                "Ask about repositories, issues, pull requests, or anything its tools can answer."
                    .to_string(),
                String::new(),
            ],
            input: String::new(),
            scroll: 0,
            follow: true,
            thinking: false,
        }
    }

    fn max_scroll(&self, terminal_width: u16, terminal_height: u16) -> u16 {
        let width = terminal_width.saturating_sub(4).max(1) as usize;
        let viewport = terminal_height.saturating_sub(9) as usize;
        wrap_output_lines(&self.lines, width)
            .len()
            .saturating_sub(viewport)
            .min(u16::MAX as usize) as u16
    }

    fn follow_tail(&mut self, terminal_width: u16, terminal_height: u16) {
        if self.follow {
            self.scroll = self.max_scroll(terminal_width, terminal_height);
        }
    }
}

fn chat_with_runner(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    cwd: &Path,
    url: &str,
) -> Result<()> {
    let mut chat = ChatView::new();
    loop {
        let Some(message) = read_chat_input(terminal, &mut chat)? else {
            return Ok(());
        };
        chat.lines.push("You".to_string());
        chat.lines.push(message.clone());
        chat.lines.push(String::new());
        chat.lines.push("Agent".to_string());
        chat.thinking = true;
        chat.follow = true;
        let argv = [
            "skill".to_string(),
            "message".to_string(),
            message,
            "--url".to_string(),
            url.to_string(),
        ];
        run_chat_turn(terminal, cwd, &argv, &mut chat)?;
        chat.thinking = false;
        chat.lines.push(String::new());
    }
}

fn read_chat_input(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    chat: &mut ChatView,
) -> Result<Option<String>> {
    chat.input.clear();
    loop {
        let size = terminal.size()?;
        chat.follow_tail(size.width, size.height);
        terminal.draw(|frame| draw_chat_view(frame, chat))?;
        let Event::Key(key) = event::read()? else {
            continue;
        };
        match (key.code, key.modifiers) {
            (KeyCode::Esc, _) | (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(None),
            (KeyCode::Enter, _) if !chat.input.trim().is_empty() => {
                return Ok(Some(chat.input.trim().to_string()));
            }
            (KeyCode::Backspace, _) => {
                chat.input.pop();
            }
            (KeyCode::PageUp, _) => {
                chat.follow = false;
                chat.scroll = chat.scroll.saturating_sub(10);
            }
            (KeyCode::PageDown, _) => {
                chat.follow = false;
                chat.scroll = chat
                    .scroll
                    .saturating_add(10)
                    .min(chat.max_scroll(size.width, size.height));
            }
            (KeyCode::End, _) => chat.follow = true,
            (KeyCode::Char(ch), _) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                chat.input.push(ch);
            }
            _ => {}
        }
    }
}

fn run_chat_turn(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    cwd: &Path,
    argv: &[String],
    chat: &mut ChatView,
) -> Result<()> {
    let mut child = Command::new(std::env::current_exe().context("resolving current executable")?)
        .args(argv)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("sending message to the GitHub agent")?;
    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take().context("capturing agent response")?;
    let stderr = child.stderr.take().context("capturing agent diagnostics")?;
    let readers = [stdout_reader(stdout, tx.clone()), stderr_reader(stderr, tx)];
    let mut canceled = false;

    loop {
        while let Ok(line) = rx.try_recv() {
            chat.lines.push(line);
        }
        let size = terminal.size()?;
        chat.follow_tail(size.width, size.height);
        terminal.draw(|frame| draw_chat_view(frame, chat))?;
        if event::poll(Duration::from_millis(50))? {
            if let Event::Key(key) = event::read()? {
                match (key.code, key.modifiers) {
                    (KeyCode::Char('c'), KeyModifiers::CONTROL) => {
                        child.kill().context("canceling agent response")?;
                        canceled = true;
                    }
                    (KeyCode::PageUp, _) => {
                        chat.follow = false;
                        chat.scroll = chat.scroll.saturating_sub(10);
                    }
                    (KeyCode::PageDown, _) => {
                        chat.follow = false;
                        chat.scroll = chat
                            .scroll
                            .saturating_add(10)
                            .min(chat.max_scroll(size.width, size.height));
                    }
                    (KeyCode::End, _) => chat.follow = true,
                    _ => {}
                }
            }
        }
        if let Some(status) = child.try_wait().context("waiting for agent response")? {
            for reader in readers {
                let _ = reader.join();
            }
            while let Ok(line) = rx.try_recv() {
                chat.lines.push(line);
            }
            if canceled {
                chat.lines.push("Response canceled.".to_string());
                return Ok(());
            }
            if !status.success() {
                anyhow::bail!("agent response exited with {status}");
            }
            return Ok(());
        }
    }
}

fn run_agentos_in_tui(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    argv: &[String],
    cwd: &Path,
    view: &mut RunView,
) -> Result<()> {
    view.push(format!("$ {}", render_command(argv)));
    view.push("");
    view.running = true;
    let mut child = Command::new(std::env::current_exe().context("resolving current executable")?)
        .args(argv)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("running {}", render_command(argv)))?;

    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take().context("capturing agentos stdout")?;
    let stderr = child.stderr.take().context("capturing agentos stderr")?;
    let readers = [stdout_reader(stdout, tx.clone()), stderr_reader(stderr, tx)];
    let mut canceled = false;

    let status_result = (|| -> Result<std::process::ExitStatus> {
        loop {
            while let Ok(line) = rx.try_recv() {
                view.push(line);
            }
            let size = terminal.size()?;
            view.follow_tail(size.width, size.height);
            terminal.draw(|frame| {
                draw(frame, app);
                draw_run_view(frame, view);
            })?;

            if event::poll(Duration::from_millis(50))? {
                if let Event::Key(key) = event::read()? {
                    match (key.code, key.modifiers) {
                        (KeyCode::Char('c'), KeyModifiers::CONTROL) => {
                            child.kill().context("canceling agentos command")?;
                            canceled = true;
                        }
                        (KeyCode::Up | KeyCode::Char('k'), _) => {
                            view.scroll_up(1, size.width, size.height);
                        }
                        (KeyCode::Down | KeyCode::Char('j'), _) => {
                            view.scroll_down(1, size.width, size.height);
                        }
                        (KeyCode::End, _) => view.follow = true,
                        _ => {}
                    }
                }
            }
            if let Some(status) = child.try_wait().context("waiting for agentos command")? {
                break Ok(status);
            }
        }
    })();

    if status_result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }

    for reader in readers {
        let _ = reader.join();
    }
    while let Ok(line) = rx.try_recv() {
        view.push(line);
    }
    view.running = false;
    view.push("");
    let status = status_result?;
    if canceled {
        view.push("Canceled by user.");
        anyhow::bail!("{} was canceled", render_command(argv));
    }
    if !status.success() {
        view.push(format!("Exited with {status}."));
        anyhow::bail!("{} exited with {status}", render_command(argv));
    }
    view.push("Completed successfully.");
    view.push("");
    Ok(())
}

fn stdout_reader(
    stdout: std::process::ChildStdout,
    tx: mpsc::Sender<String>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || read_output(stdout, tx))
}

fn stderr_reader(
    stderr: std::process::ChildStderr,
    tx: mpsc::Sender<String>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || read_output(stderr, tx))
}

fn read_output(reader: impl io::Read, tx: mpsc::Sender<String>) {
    for line in BufReader::new(reader).lines() {
        match line {
            Ok(line) => {
                if tx.send(line).is_err() {
                    break;
                }
            }
            Err(err) => {
                let _ = tx.send(format!("Unable to read command output: {err}"));
                break;
            }
        }
    }
}

fn show_run_view(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &App,
    view: &mut RunView,
) -> Result<()> {
    view.running = false;
    loop {
        let size = terminal.size()?;
        view.follow_tail(size.width, size.height);
        terminal.draw(|frame| {
            draw(frame, app);
            draw_run_view(frame, view);
        })?;
        let Event::Key(key) = event::read()? else {
            continue;
        };
        match (key.code, key.modifiers) {
            (KeyCode::Enter | KeyCode::Esc | KeyCode::Char('q'), _) => return Ok(()),
            (KeyCode::Char('c'), KeyModifiers::CONTROL) => return Ok(()),
            (KeyCode::Up | KeyCode::Char('k'), _) => {
                view.scroll_up(1, size.width, size.height);
            }
            (KeyCode::Down | KeyCode::Char('j'), _) => {
                view.scroll_down(1, size.width, size.height);
            }
            (KeyCode::PageUp, _) => {
                view.scroll_up(10, size.width, size.height);
            }
            (KeyCode::PageDown, _) => {
                view.scroll_down(10, size.width, size.height);
            }
            (KeyCode::Home, _) => {
                view.follow = false;
                view.scroll = 0;
            }
            (KeyCode::End, _) => view.follow = true,
            _ => {}
        }
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
        RecipeKind::Tui(TuiAction::SaveSecret) => {
            lines.push(Line::from(Span::styled(
                "TUI prompts",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.push(Line::from("1. Choose a common secret name or Custom"));
            lines.push(Line::from("2. Secret value, hidden while typing"));
            lines.push(Line::from("3. Save to the OS credential store"));
            lines.push(Line::from(""));
        }
        RecipeKind::Tui(TuiAction::ListSecrets) => {
            lines.push(Line::from(Span::styled(
                "Saved secrets",
                Style::default().fg(Color::Yellow).bold(),
            )));
            match crate::secrets::list_names() {
                Ok(names) if names.is_empty() => {
                    lines.push(Line::from("No AgentOS secrets saved."));
                }
                Ok(names) => {
                    for name in names {
                        lines.push(Line::from(format!("{name}  (value hidden)")));
                    }
                }
                Err(err) => {
                    lines.push(Line::from(format!("Unable to read secret index: {err:#}")));
                }
            }
            lines.push(Line::from(""));
        }
        RecipeKind::Tui(TuiAction::RemoveSecret) => {
            lines.push(Line::from(Span::styled(
                "TUI prompts",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.push(Line::from("1. Choose a saved secret name or Custom"));
            lines.push(Line::from("2. Type the same name to confirm removal"));
            lines.push(Line::from("3. Remove from the OS credential store"));
            lines.push(Line::from(""));
        }
        RecipeKind::Workflow(Workflow::GithubAgentChat) => {
            lines.push(Line::from(Span::styled(
                "Interactive session",
                Style::default().fg(Color::Yellow).bold(),
            )));
            lines.push(Line::from("1. Check model and GitHub credentials"));
            lines.push(Line::from(
                "2. Start the GitHub agent with its authenticated MCP tools",
            ));
            lines.push(Line::from(
                "3. Chat with the agent for as many turns as needed",
            ));
            lines.push(Line::from("4. Stop the runner when you leave chat"));
            lines.push(Line::from(""));
            maybe_add_secret_status(&mut lines, Workflow::GithubAgentChat);
        }
    }
    if !recipe.fields.is_empty() {
        lines.push(Line::from(Span::styled(
            "Prompts before run",
            Style::default().fg(Color::Yellow).bold(),
        )));
        for field in &recipe.fields {
            let default = field.default.unwrap_or(if field.required {
                "required"
            } else {
                "optional"
            });
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

fn draw_run_view(frame: &mut Frame<'_>, view: &RunView) {
    let frame_area = frame.area();
    let area = Rect {
        x: frame_area.x.saturating_add(2),
        y: frame_area.y.saturating_add(1),
        width: frame_area.width.saturating_sub(4).max(20),
        height: frame_area.height.saturating_sub(2).max(7),
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(5), Constraint::Length(2)])
        .split(area);
    let status = if view.running { "running" } else { "finished" };
    let output_width = chunks[0].width.saturating_sub(2).max(1) as usize;
    let wrapped_lines = wrap_output_lines(&view.lines, output_width);
    let lines = wrapped_lines
        .iter()
        .map(|line| Line::from(line.as_str()))
        .collect::<Vec<_>>();
    let viewport = chunks[0].height.saturating_sub(2) as usize;
    let max_scroll = wrapped_lines
        .len()
        .saturating_sub(viewport)
        .min(u16::MAX as usize) as u16;

    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .scroll((view.scroll.min(max_scroll), 0))
            .block(
                Block::default()
                    .title(format!("{} · {status}", view.title))
                    .borders(Borders::ALL),
            ),
        chunks[0],
    );
    let help = if view.running {
        "Up/Down scroll    End follow output    Ctrl-C cancel"
    } else {
        "Up/Down or PgUp/PgDn scroll    Home/End jump    Enter return"
    };
    frame.render_widget(
        Paragraph::new(Span::styled(help, Style::default().fg(Color::Gray)))
            .alignment(Alignment::Center),
        chunks[1],
    );
}

fn draw_chat_view(frame: &mut Frame<'_>, chat: &ChatView) {
    let area = frame.area();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(5),
            Constraint::Length(3),
            Constraint::Length(2),
        ])
        .split(area);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("AgentOS", Style::default().fg(Color::Cyan).bold()),
            Span::raw("  GitHub agent"),
            if chat.thinking {
                Span::styled("  thinking...", Style::default().fg(Color::Yellow))
            } else {
                Span::raw("")
            },
        ]))
        .alignment(Alignment::Center)
        .block(Block::default().borders(Borders::ALL)),
        chunks[0],
    );

    let output_width = chunks[1].width.saturating_sub(2).max(1) as usize;
    let wrapped = wrap_output_lines(&chat.lines, output_width);
    let transcript = wrapped
        .iter()
        .map(|line| {
            let style = match line.as_str() {
                "You" => Style::default().fg(Color::Cyan).bold(),
                "Agent" => Style::default().fg(Color::Green).bold(),
                _ => Style::default(),
            };
            Line::from(Span::styled(line.as_str(), style))
        })
        .collect::<Vec<_>>();
    let viewport = chunks[1].height.saturating_sub(2) as usize;
    let max_scroll = wrapped
        .len()
        .saturating_sub(viewport)
        .min(u16::MAX as usize) as u16;
    frame.render_widget(
        Paragraph::new(Text::from(transcript))
            .scroll((chat.scroll.min(max_scroll), 0))
            .block(Block::default().title("Conversation").borders(Borders::ALL)),
        chunks[1],
    );

    let input_width = chunks[2].width.saturating_sub(4).max(1) as usize;
    let shown_input = input_window(&chat.input, false, input_width);
    let input_style = if chat.thinking {
        Style::default().fg(Color::DarkGray)
    } else {
        Style::default()
    };
    frame.render_widget(
        Paragraph::new(Span::styled(shown_input.as_str(), input_style)).block(
            Block::default()
                .title(if chat.thinking {
                    "Waiting for agent"
                } else {
                    "Message"
                })
                .borders(Borders::ALL),
        ),
        chunks[2],
    );
    if !chat.thinking {
        frame.set_cursor_position((
            chunks[2].x + 1 + UnicodeWidthStr::width(shown_input.as_str()) as u16,
            chunks[2].y + 1,
        ));
    }
    let help = if chat.thinking {
        "Ctrl-C cancel response    PgUp/PgDn scroll    End latest"
    } else {
        "Enter send    Esc leave chat    PgUp/PgDn scroll    End latest"
    };
    frame.render_widget(
        Paragraph::new(Span::styled(help, Style::default().fg(Color::Gray)))
            .alignment(Alignment::Center),
        chunks[3],
    );
}

fn run_view_dimensions(terminal_width: u16, terminal_height: u16) -> (usize, usize) {
    let area_width = terminal_width.saturating_sub(4).max(20);
    let area_height = terminal_height.saturating_sub(2).max(7);
    let body_height = area_height.saturating_sub(2);
    (
        area_width.saturating_sub(2).max(1) as usize,
        body_height.saturating_sub(2) as usize,
    )
}

fn wrap_output_lines(lines: &[String], max_width: usize) -> Vec<String> {
    let mut wrapped = Vec::new();
    for line in lines {
        if line.is_empty() {
            wrapped.push(String::new());
            continue;
        }
        let mut current = String::new();
        let mut width = 0;
        for ch in line.chars() {
            let char_width = UnicodeWidthChar::width(ch).unwrap_or(0);
            if !current.is_empty() && width + char_width > max_width {
                wrapped.push(current);
                current = String::new();
                width = 0;
            }
            current.push(ch);
            width += char_width;
        }
        wrapped.push(current);
    }
    wrapped
}

fn draw_prompt(
    frame: &mut Frame<'_>,
    title: &str,
    label: &str,
    default: Option<&str>,
    value: &str,
    secret: bool,
    allow_empty: bool,
) {
    let area = centered_rect(64, 9, frame.area());
    let input_width = area.width.saturating_sub(3) as usize;
    let shown_value = input_window(value, secret, input_width);
    let guidance = match default {
        Some(default) => format!("Default: {default}"),
        None if secret => "Input is hidden while typing".to_string(),
        None if allow_empty => "Optional: press Enter to skip".to_string(),
        None => "Type a value".to_string(),
    };
    let submit_help = if default.is_some() {
        "Enter use default    Esc cancel"
    } else if allow_empty {
        "Enter accept or skip    Esc cancel"
    } else {
        "Enter accept    Esc cancel"
    };
    let body = Text::from(vec![
        Line::from(Span::styled(label, Style::default().bold())),
        Line::from(Span::styled(guidance, Style::default().fg(Color::Gray))),
        Line::from(if shown_value.is_empty() {
            Span::styled(" ", Style::default().fg(Color::Gray))
        } else {
            Span::raw(&shown_value)
        }),
        Line::from(""),
        Line::from(Span::styled(submit_help, Style::default().fg(Color::Gray))),
    ]);
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(body)
            .wrap(Wrap { trim: false })
            .block(Block::default().title(title).borders(Borders::ALL)),
        area,
    );
    frame.set_cursor_position((
        area.x + 1 + UnicodeWidthStr::width(shown_value.as_str()) as u16,
        area.y + 3,
    ));
}

fn input_window(value: &str, secret: bool, max_width: usize) -> String {
    if secret {
        return "*".repeat(value.chars().count().min(max_width));
    }

    let mut width = 0;
    let mut chars = Vec::new();
    for ch in value.chars().rev() {
        let char_width = UnicodeWidthChar::width(ch).unwrap_or(0);
        if width + char_width > max_width {
            break;
        }
        width += char_width;
        chars.push(ch);
    }
    chars.into_iter().rev().collect()
}

fn draw_select_prompt<T>(
    frame: &mut Frame<'_>,
    title: &str,
    label: &str,
    choices: &[SelectChoice<T>],
    selected: usize,
) {
    let height = (choices.len() as u16)
        .saturating_mul(2)
        .saturating_add(6)
        .min(18);
    let area = centered_rect(74, height, frame.area());
    let mut lines = vec![
        Line::from(label.to_string()),
        Line::from(Span::styled(
            "Up/Down move    Enter choose    Esc cancel",
            Style::default().fg(Color::Gray),
        )),
        Line::from(""),
    ];
    for (idx, choice) in choices.iter().enumerate() {
        let focused = idx == selected;
        let marker = if focused { ">" } else { " " };
        let style = if focused {
            Style::default().fg(Color::Black).bg(Color::Green)
        } else {
            Style::default()
        };
        lines.push(Line::from(Span::styled(
            format!("{marker} {}. {}", idx + 1, choice.label),
            style,
        )));
        lines.push(Line::from(Span::styled(
            format!("     {}", choice.description),
            Style::default().fg(Color::Gray),
        )));
    }
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .wrap(Wrap { trim: false })
            .block(Block::default().title(title).borders(Borders::ALL)),
        area,
    );
}

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width.saturating_sub(2)).max(20);
    let height = height.min(area.height.saturating_sub(2)).max(7);
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    }
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
            target: "skill",
            title: "Chat with GitHub agent",
            description: "Start the GitHub MCP bundle and have a live conversation with the agent.",
            kind: RecipeKind::Workflow(Workflow::GithubAgentChat),
            args: vec![],
            fields: vec![],
            notes: &[
                "Requires a saved or environment model credential: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or AGENTOS_CREDENTIALS.",
                "Requires a saved or environment GITHUB_PERSONAL_ACCESS_TOKEN; the value is forwarded by name and not placed in argv.",
                "The runner stays up for a multi-turn conversation and stops when you leave chat.",
            ],
        },
        Recipe {
            target: "secrets",
            title: "Save secret",
            description: "Store a local secret in the OS credential store with hidden input.",
            kind: RecipeKind::Tui(TuiAction::SaveSecret),
            args: vec![],
            fields: vec![],
            notes: &[
                "The value is prompted with hidden input and saved in the OS credential store.",
                "Choose a common env var or enter any env-style custom name.",
            ],
        },
        Recipe {
            target: "secrets",
            title: "List saved secrets",
            description: "List saved AgentOS secret names without printing values.",
            kind: RecipeKind::Tui(TuiAction::ListSecrets),
            args: vec![],
            fields: vec![],
            notes: &["Only names are listed; secret values stay in the OS credential store."],
        },
        Recipe {
            target: "secrets",
            title: "Remove secret",
            description: "Remove a saved secret from the OS credential store.",
            kind: RecipeKind::Tui(TuiAction::RemoveSecret),
            args: vec![],
            fields: vec![],
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
    fn skill_target_includes_github_agent_chat() {
        let app = App::new();
        let idx = app
            .targets
            .iter()
            .position(|target| *target == "skill")
            .expect("skill target exists");
        let mut app = app;
        app.target_idx = idx;
        let titles: Vec<&str> = app
            .visible_indices()
            .iter()
            .map(|idx| app.recipes[*idx].title)
            .collect();
        assert!(titles.contains(&"Chat with GitHub agent"));
    }

    #[test]
    fn secret_actions_stay_inside_tui() {
        let app = App::new();
        for (title, action) in [
            ("Save secret", TuiAction::SaveSecret),
            ("List saved secrets", TuiAction::ListSecrets),
            ("Remove secret", TuiAction::RemoveSecret),
        ] {
            let recipe = app
                .recipes
                .iter()
                .find(|recipe| recipe.title == title)
                .unwrap_or_else(|| panic!("{title} recipe exists"));
            assert!(matches!(&recipe.kind, RecipeKind::Tui(actual) if *actual == action));
            assert!(recipe.args.is_empty());
        }
    }

    #[test]
    fn secret_name_choices_include_custom_and_do_not_default_to_github() {
        let choices = secret_name_choices_for(&BTreeSet::new());
        assert!(matches!(
            choices.first().map(|choice| &choice.value),
            Some(SecretNameChoice::Name(name)) if name == "ANTHROPIC_API_KEY"
        ));
        assert!(choices
            .iter()
            .any(|choice| choice.value == SecretNameChoice::Custom));
        assert!(choices
            .iter()
            .any(|choice| matches!(&choice.value, SecretNameChoice::Name(name) if name == "OPENAI_API_KEY")));
        assert!(choices.iter().any(
            |choice| matches!(&choice.value, SecretNameChoice::Name(name) if name == "GITHUB_PERSONAL_ACCESS_TOKEN")
        ));
    }

    #[test]
    fn secret_name_choices_mark_saved_common_keys() {
        let saved = BTreeSet::from(["OPENAI_API_KEY".to_string()]);
        let choices = secret_name_choices_for(&saved);
        let openai = choices
            .iter()
            .find(|choice| {
                matches!(&choice.value, SecretNameChoice::Name(name) if name == "OPENAI_API_KEY")
            })
            .expect("OpenAI choice exists");
        let anthropic = choices
            .iter()
            .find(|choice| {
                matches!(&choice.value, SecretNameChoice::Name(name) if name == "ANTHROPIC_API_KEY")
            })
            .expect("Anthropic choice exists");

        assert_eq!(openai.label, "OPENAI_API_KEY ✓");
        assert!(openai.description.ends_with("(saved)"));
        assert_eq!(anthropic.label, "ANTHROPIC_API_KEY");
    }

    #[test]
    fn input_window_keeps_the_caret_end_visible() {
        assert_eq!(input_window("abcdefgh", false, 5), "defgh");
        assert_eq!(input_window("ab界", false, 3), "b界");
        assert_eq!(input_window("secret", true, 4), "****");
    }

    #[test]
    fn command_output_wraps_without_losing_wide_characters() {
        let lines = vec!["abcdef".to_string(), "ab界cd".to_string(), String::new()];
        assert_eq!(
            wrap_output_lines(&lines, 4),
            vec!["abcd", "ef", "ab界", "cd", ""]
        );
    }

    #[test]
    fn credential_status_uses_saved_names_without_secret_reads() {
        let saved = BTreeSet::from([
            "ANTHROPIC_API_KEY".to_string(),
            "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
        ]);
        assert_eq!(model_credential_status(&saved), "available");
        assert_eq!(
            secret_status("GITHUB_PERSONAL_ACCESS_TOKEN", &saved),
            "saved"
        );
        assert_eq!(secret_status("OPENAI_API_KEY", &saved), "missing");
    }

    #[test]
    fn first_scroll_up_moves_off_the_output_tail() {
        let mut view = RunView::new("test");
        for idx in 0..30 {
            view.push(format!("line {idx}"));
        }
        view.follow_tail(80, 24);
        let bottom = view.scroll;

        view.scroll_up(1, 80, 24);

        assert!(!view.follow);
        assert_eq!(view.scroll, bottom - 1);
    }

    #[test]
    fn interactive_routes_do_not_restore_the_regular_terminal_mid_session() {
        let source = include_str!("interactive.rs");
        let old_suspend_path = ["suspend", "_terminal"].concat();
        let old_line_prompt = ["prompt", "_field"].concat();
        assert!(!source.contains(&old_suspend_path));
        assert!(!source.contains(&old_line_prompt));
    }
}
