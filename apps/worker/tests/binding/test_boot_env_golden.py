"""The wire did not move: `binding.boot_env` frozen against today's real output.

#488 moves the boot env's DECLARATION into ``aci_protocol.BootEnv`` and leaves
the WIRE byte-identical. This module is the proof of the second half. The golden
dicts below were captured from the shipped ``boot_env`` before the conversion and
are asserted whole, so a key that quietly appears, vanishes, or changes value
fails here rather than in a sandbox that boots fine and drops a feature.

The one intended difference is ``AGENTOS_AGENT_ID``, which is deleted: the worker
writes it into every claim and no consumer has ever read it (AC4).

Three shapes are pinned, because they are the three the binding actually renders:

* a plain bound run on the default WorkerConfig,
* a fully loaded run (per-agent model, permission gates, connector secrets,
  credentials, base URL, fake model),
* the no-api-key fake/local path, where no state token is minted so neither the
  memory nor the history token is set (the pre-#410 no-key path).

The nondeterministic values (the per-claim runner token, the time-bounded state
token) are lifted out and asserted on their own terms, not frozen.

No mocks and no DB: ``boot_env`` makes no engine call, so a bare resolver with a
real WorkerConfig exercises the real code path end to end.
"""

from __future__ import annotations

import uuid

from agentos_worker.binding import BindingResolver, ResolvedDeployment, inject_connector_secrets
from agentos_worker.config import WorkerConfig
from agentos_worker.sandbox_token import verify

_AGENT = uuid.UUID("11111111-1111-4111-8111-111111111111")
_THREAD = "thread-1"

# The state token's exp is a wall-clock stamp, so the token bytes are pinned by
# verify() rather than frozen. The runner token is minted fresh per claim.
_MINTED = (
    "AGENTOS_RUNNER_TOKEN",
    "AGENTOS_MEMORY_TOKEN",
    "AGENTOS_HISTORY_TOKEN",
    "AGENTOS_STATE_TOKEN",
)

_MEMORY_REF = f"http://localhost:8000/agents/{_AGENT}/state/memory"
_HISTORY_REF = f"http://localhost:8000/agents/{_AGENT}/state/transcript/{_THREAD}"
_STATE_URL = f"http://localhost:8000/agents/{_AGENT}/state"


def _resolved(**kwargs: object) -> ResolvedDeployment:
    base: dict[str, object] = {
        "agent_id": _AGENT,
        "version_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "version_label": "v1",
        "bundle_ref": "bundles/x.zip",
        "max_usd_per_day": None,
        "max_output_tokens_per_run": None,
    }
    base.update(kwargs)
    return ResolvedDeployment(**base)  # type: ignore[arg-type]


def _boot_env(config: WorkerConfig, resolved: ResolvedDeployment) -> dict[str, str]:
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = config  # type: ignore[attr-defined]
    return resolver.boot_env(resolved, _THREAD)


def _split_minted(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    minted = {k: env[k] for k in _MINTED if k in env}
    return {k: v for k, v in env.items() if k not in _MINTED}, minted


def test_plain_bound_run_renders_the_frozen_boot_env() -> None:
    stable, minted = _split_minted(_boot_env(WorkerConfig(), _resolved()))

    assert stable == {
        "AGENTOS_BUDGET": (
            '{"max_output_tokens_per_run":100000,"task_budget_hint":null,"max_usd_per_day":10.0}'
        ),
        "AGENTOS_SESSION_ID": f"agent-{_AGENT}-thread-{_THREAD}",
        "AGENTOS_PLUGIN_DIR": "/bundles/current",
        "AGENTOS_BUNDLE_REF": "bundles/x.zip",
        "AGENTOS_MEMORY_REF": _MEMORY_REF,
        "AGENTOS_HISTORY_REF": _HISTORY_REF,
        "AGENTOS_STATE_URL": _STATE_URL,
    }

    assert set(minted) == set(_MINTED)
    assert len(minted["AGENTOS_RUNNER_TOKEN"]) >= 32
    # Two scopes (ADR-0033, #410, #249): the memory/history loaders share the
    # BROAD ``state`` token (they must reach the reserved namespaces to
    # rehydrate); the bundle-facing state token is the NARROW ``state.app`` token,
    # a distinct credential the API refuses on memory/transcript. They must NOT be
    # the same token, or the bundle would hold the loaders' broad reach.
    assert minted["AGENTOS_MEMORY_TOKEN"] == minted["AGENTOS_HISTORY_TOKEN"]
    assert minted["AGENTOS_STATE_TOKEN"] != minted["AGENTOS_MEMORY_TOKEN"]

    broad = minted["AGENTOS_MEMORY_TOKEN"]
    app = minted["AGENTOS_STATE_TOKEN"]
    # The loaders' token verifies as broad ``state``; the bundle's as ``state.app``
    # -- and each fails the OTHER's scope, so the server-side reserved-namespace
    # guard (which keys off exactly this distinction) cannot be spoofed.
    assert verify(broad, "agentos-dev-key", agent=str(_AGENT), scope="state") is True
    assert verify(broad, "agentos-dev-key", agent=str(_AGENT), scope="state.app") is False
    assert verify(app, "agentos-dev-key", agent=str(_AGENT), scope="state.app") is True
    assert verify(app, "agentos-dev-key", agent=str(_AGENT), scope="state") is False
    # Prove binding, not just well-formedness: both fail for any other agent.
    wrong_agent = str(uuid.UUID("33333333-3333-4333-8333-333333333333"))
    assert verify(broad, "agentos-dev-key", agent=wrong_agent, scope="state") is False
    assert verify(app, "agentos-dev-key", agent=wrong_agent, scope="state.app") is False


def test_state_refs_are_minted_from_the_runner_facing_base() -> None:
    """#678: AGENTOS_MEMORY_REF/AGENTOS_HISTORY_REF are dereferenced by the
    RUNNER, so they must be built from the runner-facing API base, never the
    worker's self-dial URL.

    In the docker substrate the worker runs host-net and self-dials the API at a
    published localhost port, while the runner it spawns lives on the bridge
    runner network and reaches the API only by its in-network service name. A ref
    minted from the worker's localhost base is unreachable from the runner, which
    boots "without memory/history" every spawn. When runner_api_base_url is set,
    both refs must carry that host and not the localhost self-dial base.
    """
    config = WorkerConfig(
        api_base_url="http://localhost:28000",
        runner_api_base_url="http://agentos-api:8000",
    )
    env = _boot_env(config, _resolved())

    assert env["AGENTOS_MEMORY_REF"] == (
        f"http://agentos-api:8000/agents/{_AGENT}/state/memory"
    )
    assert env["AGENTOS_HISTORY_REF"] == (
        f"http://agentos-api:8000/agents/{_AGENT}/state/transcript/{_THREAD}"
    )
    # The worker's own self-dial localhost base must not leak into either ref.
    assert "localhost:28000" not in env["AGENTOS_MEMORY_REF"]
    assert "localhost:28000" not in env["AGENTOS_HISTORY_REF"]


def test_state_refs_fall_back_to_the_self_dial_base_when_undivided() -> None:
    """With runner_api_base_url unset the runner reaches the API at the worker's
    own URL (k8s in-cluster, single-host local), so the refs are unchanged."""
    env = _boot_env(WorkerConfig(api_base_url="http://in-cluster-api:8000"), _resolved())

    assert env["AGENTOS_MEMORY_REF"] == (
        f"http://in-cluster-api:8000/agents/{_AGENT}/state/memory"
    )
    assert env["AGENTOS_HISTORY_REF"] == (
        f"http://in-cluster-api:8000/agents/{_AGENT}/state/transcript/{_THREAD}"
    )


def test_fully_loaded_run_renders_the_frozen_boot_env() -> None:
    config = WorkerConfig(
        fake_model=True,
        credentials="cred-1",
        model_base_url="http://ollama:11434",
        model="worker-default",
    )
    resolved = _resolved(
        model="agent-pinned",
        approval_required_tools=["Bash", "Write"],
        secrets={"GITHUB_TOKEN": "ghp-1", "JIRA_TOKEN": "jira-1"},
    )
    stable, minted = _split_minted(_boot_env(config, resolved))

    assert stable == {
        "AGENTOS_BUDGET": (
            '{"max_output_tokens_per_run":100000,"task_budget_hint":null,"max_usd_per_day":10.0}'
        ),
        "AGENTOS_SESSION_ID": f"agent-{_AGENT}-thread-{_THREAD}",
        "AGENTOS_PLUGIN_DIR": "/bundles/current",
        "AGENTOS_BUNDLE_REF": "bundles/x.zip",
        "AGENTOS_APPROVAL_REQUIRED_TOOLS": "Bash,Write",
        "AGENTOS_MEMORY_REF": _MEMORY_REF,
        "AGENTOS_HISTORY_REF": _HISTORY_REF,
        "AGENTOS_STATE_URL": _STATE_URL,
        # Connector secret values ride the merged dict by value, and the marker
        # names exactly the keys injected (#429).
        "GITHUB_TOKEN": "ghp-1",
        "JIRA_TOKEN": "jira-1",
        "AGENTOS_CONNECTOR_SECRET_KEYS": "GITHUB_TOKEN,JIRA_TOKEN",
        "AGENTOS_FAKE_MODEL": "1",
        "AGENTOS_CREDENTIALS": "cred-1",
        "ANTHROPIC_BASE_URL": "http://ollama:11434",
        # The per-agent pin (#254) wins over the worker default.
        "AGENTOS_MODEL": "agent-pinned",
    }
    assert set(minted) == set(_MINTED)


def test_no_api_key_path_mints_no_state_token() -> None:
    # fake/local: nothing to sign with, so no state token is set and the pre-#410
    # no-key path is preserved. The state URL is not a credential, so it is still
    # emitted (the store is simply unauthenticated on this path).
    stable, minted = _split_minted(_boot_env(WorkerConfig(api_key=""), _resolved()))

    assert stable == {
        "AGENTOS_BUDGET": (
            '{"max_output_tokens_per_run":100000,"task_budget_hint":null,"max_usd_per_day":10.0}'
        ),
        "AGENTOS_SESSION_ID": f"agent-{_AGENT}-thread-{_THREAD}",
        "AGENTOS_PLUGIN_DIR": "/bundles/current",
        "AGENTOS_BUNDLE_REF": "bundles/x.zip",
        "AGENTOS_MEMORY_REF": _MEMORY_REF,
        "AGENTOS_HISTORY_REF": _HISTORY_REF,
        "AGENTOS_STATE_URL": _STATE_URL,
    }
    assert set(minted) == {"AGENTOS_RUNNER_TOKEN"}


def test_boot_env_omits_agent_id() -> None:
    """AGENTOS_AGENT_ID is written by the worker and read by nobody (AC4).

    The agent's identity already reaches the sandbox inside AGENTOS_SESSION_ID
    and both state refs, so its removal costs the runner nothing.
    """

    env = _boot_env(WorkerConfig(), _resolved())

    assert "AGENTOS_AGENT_ID" not in env
    assert env["AGENTOS_SESSION_ID"] == f"agent-{_AGENT}-thread-{_THREAD}"
    assert str(_AGENT) in env["AGENTOS_MEMORY_REF"]
    assert str(_AGENT) in env["AGENTOS_HISTORY_REF"]


def test_boot_env_never_writes_the_substrate_authoritative_keys() -> None:
    """The pod name IS the sandbox id, and the chart owns the port.

    ``envVarsInjectionPolicy: Overrides`` means a worker-emitted value REPLACES
    the substrate's, so a write here would break trace stamping rather than lose
    a race with it.
    """

    env = _boot_env(WorkerConfig(), _resolved(secrets={"GITHUB_TOKEN": "ghp-1"}))

    assert "AGENTOS_SANDBOX_ID" not in env
    assert "AGENTOS_RUNNER_PORT" not in env


def test_boot_env_never_writes_the_kernel_owned_resume_keys() -> None:
    """The resume overlay is the kernel's; the binding must not pre-seed it.

    A binding-written grant would hand the boot turn a standing allowance on
    every claim, not just a genuinely approved resume (#430, #544).
    """

    env = _boot_env(WorkerConfig(), _resolved(approval_required_tools=["Bash"]))

    assert "AGENTOS_APPROVAL_GRANT_TOOL" not in env
    assert "AGENTOS_APPROVAL_RESUMED_KIND" not in env


def test_connector_secret_with_a_reserved_name_is_dropped_and_unmarked() -> None:
    """#457: the filter is name-policy-based, so it holds on this path too.

    This lane no longer proves order-independence: ``render_worker`` sets
    ANTHROPIC_BASE_URL BEFORE the injection loop, so an ``if name not in env``
    regression would be masked here rather than caught. The ordering-sensitive
    guard lives in the eval lane, which still injects before it applies the model
    env -- see ``test_eval_boot_env_drops_reserved_connector_secret``
    (apps/worker/tests/eval/test_stream.py). What this test pins is that the
    worker lane drops a reserved-name secret at all, and that a dropped key
    carries neither its value nor a marker entry.
    """

    env = _boot_env(
        WorkerConfig(credentials="cred-1"),
        _resolved(
            secrets={
                "ANTHROPIC_BASE_URL": "http://attacker.example",
                "AGENTOS_CREDENTIALS": "stolen",
                "GITHUB_TOKEN": "ghp-1",
            }
        ),
    )

    assert "ANTHROPIC_BASE_URL" not in env
    assert env["AGENTOS_CREDENTIALS"] == "cred-1"
    # A dropped key never carries its value and never enters the marker, so the
    # k8s substrate does not strip a key that was never injected.
    assert env["AGENTOS_CONNECTOR_SECRET_KEYS"] == "GITHUB_TOKEN"
    assert env["GITHUB_TOKEN"] == "ghp-1"


def test_marker_is_absent_when_every_secret_was_dropped() -> None:
    env = _boot_env(WorkerConfig(), _resolved(secrets={"AGENTOS_RUNNER_TOKEN": "forged"}))

    assert "AGENTOS_CONNECTOR_SECRET_KEYS" not in env
    assert env["AGENTOS_RUNNER_TOKEN"] != "forged"


def test_marker_is_absent_when_the_agent_has_no_secrets() -> None:
    assert "AGENTOS_CONNECTOR_SECRET_KEYS" not in _boot_env(WorkerConfig(), _resolved())


def test_inject_connector_secrets_is_the_markers_sole_writer() -> None:
    """The render surface must leave the marker to the injection loop (#429).

    If the boot-env renderer also emitted the marker, the two writers would race
    to describe which keys are secrets, and a wrong marker means the k8s
    substrate either persists a plaintext secret in etcd or strips a key the
    runner needs.
    """

    rendered = _boot_env(WorkerConfig(), _resolved())
    assert "AGENTOS_CONNECTOR_SECRET_KEYS" not in rendered

    inject_connector_secrets(rendered, {"GITHUB_TOKEN": "ghp-1"}, agent_label=_AGENT)
    assert rendered["AGENTOS_CONNECTOR_SECRET_KEYS"] == "GITHUB_TOKEN"
