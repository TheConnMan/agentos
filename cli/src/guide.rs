//! `agentos guide`: the self-describing harness primer (ADR-0021, issue #322).
//!
//! Emits a compact, self-contained "how to drive this harness" document for a
//! coding agent, modeled on `SKILL.md`: ordered by what the agent needs first,
//! roughly 100 lines, carrying only non-discoverable knowledge -- the parity
//! ladder, the when/which decision logic, the landmines, and verify-first. It
//! deliberately omits a directory tour the agent could derive itself (ADR-0021
//! decision 2: restate-the-obvious guidance measurably hurts).
//!
//! One data model ([`primer`]) is the single source of truth; the Markdown
//! default and the `--json` structured variant both render from it, so a command
//! printed by one is byte-identical in the other and the drift test in
//! `cli/tests/guide.rs` can validate the printed commands against the CLI's own
//! `agentos schema` manifest. Every printed `agentos ...` invocation is a real
//! command path -- prose refers to the product as "AgentOS".

use anyhow::Result;
use serde::Serialize;

use crate::ui;

/// The whole primer as data. Serialized directly for `--json`; rendered to
/// Markdown for the default. Fields are ordered as the agent reads them.
#[derive(Serialize)]
pub struct Primer {
    pub harness: &'static str,
    pub summary: &'static str,
    pub verify_first: VerifyFirst,
    pub parity_ladder: Vec<Rung>,
    pub decision_logic: Vec<Decision>,
    pub landmines: Vec<Landmine>,
    pub recovery: Vec<Recovery>,
}

#[derive(Serialize)]
pub struct VerifyFirst {
    pub why: &'static str,
    pub commands: Vec<&'static str>,
}

/// One rung of the parity ladder: a real, runnable command and why to run it.
#[derive(Serialize)]
pub struct Rung {
    pub tier: &'static str,
    pub command: &'static str,
    pub purpose: &'static str,
}

#[derive(Serialize)]
pub struct Decision {
    pub question: &'static str,
    pub answer: &'static str,
}

#[derive(Serialize)]
pub struct Landmine {
    pub title: &'static str,
    pub detail: &'static str,
}

#[derive(Serialize)]
pub struct Recovery {
    pub symptom: &'static str,
    pub fix: &'static str,
}

/// The authored primer. Content lives here so Markdown and JSON never diverge.
pub fn primer() -> Primer {
    Primer {
        harness: "agentos",
        summary: "AgentOS is a harness: it guarantees that a bundle behaving in a local chat \
                  behaves identically as a deployed local process and again on Kubernetes. You \
                  author the skill; the harness owns deployment parity.",
        verify_first: VerifyFirst {
            why: "Your training data is stale. Confirm a command exists before you run it -- \
                  never invoke one you have not seen in the manifest or --help.",
            commands: vec!["agentos schema", "agentos skill --help"],
        },
        parity_ladder: vec![
            Rung {
                tier: "init",
                command: "agentos init deal-desk",
                purpose: "Scaffold a bundle (Claude Code plugin shape) with an evals/cases.json seed.",
            },
            Rung {
                tier: "skill",
                command: "agentos skill up --fake-model",
                purpose: "Boot the runner alone, offline, no credential -- the fastest authoring loop.",
            },
            Rung {
                tier: "skill",
                command: "agentos skill eval",
                purpose: "Run evals/cases.json in-process. This is the promotion gate; it must be green.",
            },
            Rung {
                tier: "skill",
                command: "agentos skill message \"hello\"",
                purpose: "Drive one synthetic turn and stream the reply.",
            },
            Rung {
                tier: "skill",
                command: "agentos skill down",
                purpose: "Stop and remove the runner.",
            },
            Rung {
                tier: "local",
                command: "agentos local up",
                purpose: "Bring up the full platform via compose (queue, worker, sandbox), still zero Slack.",
            },
            Rung {
                tier: "local",
                command: "agentos local deploy",
                purpose: "Push the identical bundle to the local platform API.",
            },
            Rung {
                tier: "local",
                command: "agentos local message \"hello\"",
                purpose: "Drive the real product loop end to end -- the path a Slack mention would take.",
            },
            Rung {
                tier: "cluster",
                command: "agentos cluster up",
                purpose: "Install the release on Kubernetes via Helm.",
            },
            Rung {
                tier: "cluster",
                command: "agentos cluster deploy",
                purpose: "Ship the same bundle to the cluster.",
            },
            Rung {
                tier: "cluster",
                command: "agentos cluster message \"hello\"",
                purpose: "Drive the same loop on the cluster.",
            },
        ],
        decision_logic: vec![
            Decision {
                question: "skill vs local vs cluster",
                answer: "skill is the runner only (offline, no platform, no Slack) -- the tightest \
                         loop. local puts the full platform in front of the identical runner via \
                         compose. cluster is the same on Kubernetes. Pick the lightest tier that \
                         answers your question; promote only to reproduce a divergence.",
            },
            Decision {
                question: "skill vs MCP server",
                answer: "A skill is a prompt+tools capability inside the bundle; an MCP server is an \
                         external tool surface the skill calls. Add a skill for behavior the agent \
                         performs; add an MCP server to give it a new tool.",
            },
            Decision {
                question: "how an eval gates promotion",
                answer: "evals/cases.json is the contract. `agentos skill eval` must be green before \
                         you deploy; merging to main promotes to prod (git flow is the deploy model). \
                         Never promote a bundle whose evals are red.",
            },
        ],
        landmines: vec![
            Landmine {
                title: "Skill frontmatter uses `allowed-tools`, not `tools`",
                detail: "The wrong key parses but silently grants no tools.",
            },
            Landmine {
                title: "MCP servers must be inline objects in .mcp.json",
                detail: "A string-pointer declaration silently fails to load; use the bare inline object form.",
            },
            Landmine {
                title: "In-cluster sandboxes need dnsPolicy ClusterFirst",
                detail: "Otherwise the bound bundle fetch cannot resolve the in-cluster object store.",
            },
            Landmine {
                title: "secretKeyRef env vars resolve once, at pod start",
                detail: "A connect that rotates a secret must also roll the pod; `agentos cluster comms` does this for you.",
            },
            Landmine {
                title: "An empty-string API key is not \"unset\"",
                detail: "It trips the CLI auth gate. Omit the flag or env entirely to fall back, rather than passing an empty value.",
            },
        ],
        recovery: vec![
            Recovery {
                symptom: "\"platform API ... unreachable\" on a local deploy or message",
                fix: "The stack is down. Run `agentos local up`, then retry.",
            },
            Recovery {
                symptom: "authentication_failed from a live model",
                fix: "A real credential is empty or being overridden. Unset the empty one; set AGENTOS_MODEL_CREDENTIALS for `agentos cluster up`.",
            },
            Recovery {
                symptom: "\"(no response)\" or an empty reply",
                fix: "You are on the fake model (--fake-model, or a sealed install). Provide a credential to go live.",
            },
            Recovery {
                symptom: "a command \"does not exist\"",
                fix: "You trusted training over the manifest. Re-run `agentos schema` and use the confirmed spelling.",
            },
        ],
    }
}

fn tier_caption(tier: &'static str) -> &'static str {
    match tier {
        "init" => "0. Scaffold a bundle (Claude Code plugin shape + evals/cases.json seed)",
        "skill" => "1. skill tier: the runner alone, offline, fake model -- the fastest loop",
        "local" => "2. local tier: the same bundle through the full platform (compose), zero Slack",
        "cluster" => "3. cluster tier: the same bundle on Kubernetes (a Helm release)",
        other => other,
    }
}

/// Render the primer as Markdown. Every `agentos ...` token printed here is a
/// real command; the drift test enforces that against the live manifest.
fn render_markdown(p: &Primer) -> String {
    let mut s = String::new();
    s.push_str("# AgentOS harness primer\n\n");
    s.push_str(p.summary);
    s.push_str(
        "\n\nYou are a coding agent driving this harness. This primer carries only what you \
         cannot derive from the command tree or your training data. Read it before you \
         start.\n\n",
    );

    s.push_str("## Verify-first (before every command)\n\n");
    s.push_str(p.verify_first.why);
    s.push_str("\n\n");
    for c in &p.verify_first.commands {
        s.push_str(&format!("    {c}\n"));
    }
    s.push('\n');

    s.push_str("## The parity ladder (the core loop)\n\n");
    s.push_str(
        "One immutable bundle and one evals/cases.json, run at three tiers. Climb only as far \
         as you need. A tier-to-tier divergence is the harness catching a real environment \
         bug, not your skill's logic.\n\n",
    );
    let mut last_tier = "";
    for r in &p.parity_ladder {
        if r.tier != last_tier {
            s.push_str(&format!("    # {}\n", tier_caption(r.tier)));
            last_tier = r.tier;
        }
        s.push_str(&format!("    {}\n", r.command));
        s.push_str(&format!("    #   {}\n", r.purpose));
    }
    s.push('\n');
    s.push_str(
        "The eval file never changes across tiers. If `skill eval` is green but a deployed \
         message misbehaves, the bundle is fine and the environment is not -- that gap is the \
         signal the harness exists to surface.\n\n",
    );

    s.push_str("## When / which\n\n");
    for d in &p.decision_logic {
        s.push_str(&format!("- **{}**\n  {}\n\n", d.question, d.answer));
    }

    s.push_str("## Landmines (non-discoverable)\n\n");
    for l in &p.landmines {
        s.push_str(&format!("- **{}**\n  {}\n\n", l.title, l.detail));
    }

    s.push_str("## Error -> recovery\n\n");
    for r in &p.recovery {
        s.push_str(&format!("- **{}**\n  {}\n\n", r.symptom, r.fix));
    }

    s.push_str("When anything is uncertain, confirm it against `agentos schema` before you act.\n");
    s
}

/// The primer rendered to Markdown. One seam for callers that need the same
/// authored body the `agentos guide` default prints -- the scaffold's harness
/// skill renders from this so the two can never diverge (D2 anti-drift).
pub fn primer_markdown() -> String {
    render_markdown(&primer())
}

/// `agentos guide`: print the primer. Markdown to stdout by default; the global
/// `--json` flag prints the structured variant to stdout via the shared
/// machine-output path (any human text would go to stderr).
pub fn run() -> Result<()> {
    let p = primer();
    let ui = ui::ui();
    if ui.json() {
        ui.emit_json(&serde_json::to_value(&p)?);
    } else {
        ui.payload_plain(&render_markdown(&p));
    }
    Ok(())
}
