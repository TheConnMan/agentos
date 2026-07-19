#!/bin/bash
# Cold-start parity ladder for the agentos CLI (issue #690).
#
# This is an E2E test, not a gate: it drives the SAME bundle through each
# deployment tier with the tier's own real verbs and asserts a turn actually
# finalized. Rung 1 (skill) is the existing `cli/scripts/e2e.sh`, invoked as is
# so the skill leg has exactly one implementation. Rung 2 (local) is
# `local up --minimal` -> `local deploy` -> `local message` -> `local down`.
# Rung 3 (cluster) is `cluster deploy` -> `cluster message` against a release
# that is ALREADY installed; the ladder never installs or uninstalls one.
#
# What it is NOT: it is not a compose test and not a helm test. Every step goes
# through an `agentos` verb, because the point is to catch a tier whose verb
# drifted from its sibling. The one raw-docker use is the post-teardown
# assertion that nothing agentos-related survived.
#
# Blast radius of the teardown sweep: sandbox containers are matched by the
# substrate label, which is host-wide and not per-worktree, so the sweep only
# runs when THIS ladder brought the compose stack up. A stack it merely reused
# belongs to another session, and so do that session's sandboxes.
#
# Fake model by default, so a default run is credential-free even on a box that
# HAS credentials. Under AGENTOS_E2E_LIVE=1 the ladder runs the real model and
# requires a credential up front. Under fake it asserts PLUMBING only -- that a
# turn finalized and a reply came back -- never reply CONTENT (ADR-0055, #612):
# an assertion tuned to the fake's canned reply manufactures a green.
#
# Requirements: docker, a cargo toolchain (or $AGENTOS_BIN), and an
# agentos-runner image (`agentos build`). Rung 3 additionally needs a reachable
# cluster with a release installed. Run from anywhere:
#
#   bash cli/scripts/e2e-ladder.sh
#
# Env knobs:
#   AGENTOS_E2E_TIERS        comma list of rungs (default skill,local; `all` =
#                            skill,local,cluster). A NAMED tier is REQUIRED: if
#                            cluster is named and no release responds, exit 1.
#   AGENTOS_E2E_LIVE         1 = real model on rungs 2 and 3 (rung 1 stays fake)
#   AGENTOS_BIN              path to a prebuilt agentos binary (skip cargo build)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TIERS="${AGENTOS_E2E_TIERS:-skill,local}"
LIVE="${AGENTOS_E2E_LIVE:-0}"
# Fixed, not an env knob: the ladder asserts PLUMBING, so the bundle it ships is
# a fixed input of the test rather than something a caller varies.
BUNDLE_SRC="$REPO_ROOT/examples/weather"
# Hardcoded, and deliberately NOT an env knob: the stub port is the constant
# DEFAULT_LOCAL_STUB_PORT in cli/src/message.rs, pinned to the compose worker's
# SLACK_API_BASE_URL. An override would only move this script's precheck, so it
# could green-light an occupied 8155 and then hang on the message timeout.
STUB_PORT=8155
PROMPT="What is the weather in Denver right now?"
# The fake model's only reply (runner/src/agentos_runner/fake.py). It is used
# ONLY as a live-mode negative control -- "the reply must not be this" -- never
# as a pass condition. Matching it to green is the #612 bypass.
FAKE_SENTINEL="all done"

# Set once the ladder itself brought the compose stack up. The thread that
# brought a stack up owns tearing it down, so a stack that was already running
# when the ladder started is reused and left alone.
LOCAL_STACK_OWNED=0

# The label the sandbox substrate stamps on every runner container it spawns
# (cli/src/docker.rs SANDBOX_LABEL, apps/worker sandbox/types.py). Container
# NAMES are per-thread (agentos-thread-<digest>-<nonce>), so a name filter
# matches nothing; the label is the only handle that actually selects them.
SANDBOX_LABEL="agentos.dev/managed-by=agentos-sandbox-substrate"

echo "=== Resolve the agentos binary ==="
if [[ -n "${AGENTOS_BIN:-}" && -x "${AGENTOS_BIN:-}" ]]; then
    # Absolutize: the ladder invokes the binary from other directories, so a
    # relative $AGENTOS_BIN (as CI passes) must be pinned here or it stops
    # resolving later.
    BIN="$(cd "$(dirname "$AGENTOS_BIN")" && pwd)/$(basename "$AGENTOS_BIN")"
    echo "using prebuilt binary: $BIN"
else
    (cd "$REPO_ROOT/cli" && cargo build --release --quiet)
    BIN="$REPO_ROOT/cli/target/release/agentos"
fi
"$BIN" --version

WORKDIR="$(mktemp -d)"
cleanup() {
    # Capture the real exit code FIRST: a teardown command that fails must not
    # turn a red run green, and a successful teardown must not mask a red rung.
    local code=$?
    set +e
    # The compose worker spawns runner containers as SIBLINGS on the host daemon
    # via the mounted docker socket, so a rung that died before `local down` can
    # strand them. This raw sweep is a BACKSTOP, not duplication: `local down`
    # already reaps this same label itself (docker::reap_labeled in
    # cli/src/local.rs, #613) and bails loudly if the reap is incomplete, so on
    # any normal path there is nothing here to find. It exists only for the case
    # where `local down` never ran or failed, which is exactly the case that
    # strands containers. Sweep ONLY when this ladder owned the stack: the label is
    # host-wide, and force-removing another session's sandboxes would break a
    # run this one has no business touching.
    if (( LOCAL_STACK_OWNED )); then
        echo
        echo "=== teardown: agentos local down ==="
        # Tolerated failure, on top of `set +e`: the stack may never have
        # finished coming up, and a failed `local down` must not skip the
        # sandbox sweep below or change the exit code captured above.
        "$BIN" local down || echo "warning: \`local down\` failed during teardown; sweeping anyway." >&2
        local orphans
        orphans="$(docker ps -aq --filter "label=$SANDBOX_LABEL" 2>/dev/null)"
        if [[ -n "$orphans" ]]; then
            echo "sweeping orphaned sandbox containers"
            # shellcheck disable=SC2086
            docker rm -f $orphans >/dev/null 2>&1
        fi
    fi
    rm -rf "$WORKDIR"
    exit "$code"
}
trap cleanup EXIT
# Without these, a Ctrl-C or a kill can end the shell without running the EXIT
# trap, stranding a running stack on a box that cannot afford one.
trap 'exit 130' INT
trap 'exit 143' TERM

# The ONE place the fake/live asymmetry is stated. There is no shared
# fake-model control across tiers: skill takes a `--fake-model` flag, local
# reads AGENTOS_FAKE_MODEL, and cluster bakes it into the install. Keeping the
# translation here means the seam is written down once.
apply_model_mode() {
    if [[ "$LIVE" == "1" ]]; then
        if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${AGENTOS_CREDENTIALS:-}" ]]; then
            echo "error: AGENTOS_E2E_LIVE=1 needs a model credential in the environment, and none is set." >&2
            echo "fix: export ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or AGENTOS_CREDENTIALS, or drop AGENTOS_E2E_LIVE to run sealed against the fake model." >&2
            exit 1
        fi
        # Live means live: an inherited AGENTOS_FAKE_MODEL=1 would silently seal
        # a run the operator asked to be real.
        unset AGENTOS_FAKE_MODEL
        echo "model mode: LIVE (real model on the local and cluster rungs)"
    else
        # Exported, not merely defaulted: a developer shell that happens to carry
        # ANTHROPIC_API_KEY must still get the sealed run. That is what
        # credential-free-by-default means.
        export AGENTOS_FAKE_MODEL=1
        echo "model mode: FAKE (sealed; AGENTOS_FAKE_MODEL=1 exported for the local rung)"
    fi
    echo "note: the skill rung is fake either way -- cli/scripts/e2e.sh hardcodes --fake-model."
    echo "note: the cluster rung's model is a property of the installed release (cluster up --fake-model), not of this run."
}

# Accept ONLY the `reply` shape with finalized == true and a non-empty reply.
# The other three shapes in cli/schema/message.schema.json get distinct
# messages, because awaiting-approval and timed-out have different causes and a
# merged message wastes debugging time.
assert_finalized_reply() {
    local label="$1" payload="$2" verdict reply
    # stdout only: --json puts the payload on stdout and human text on stderr,
    # so a combined-stream parse fails intermittently and reads like a product bug.
    verdict="$(printf '%s' "$payload" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("unparseable")
    sys.exit(0)
if not isinstance(d, dict):
    print("unparseable")
elif d.get("dry_run"):
    print("dry_run")
elif d.get("awaiting_approval"):
    print("awaiting_approval")
elif d.get("timed_out"):
    print("timed_out")
elif d.get("finalized") is True and isinstance(d.get("reply"), str):
    # An empty reply is a distinct failure, not a parse failure: the turn
    # finalized but nothing came back, which is exactly the plumbing break
    # this assertion exists to catch.
    print("ok" if d["reply"].strip() else "empty_reply")
    print(d["reply"])
else:
    print("not_finalized")
' || echo "unparseable")"
    # Split the two-line protocol on the FIRST newline: line one is the verdict,
    # everything after it is the reply. `reply` first, because the second
    # expansion overwrites the string both read.
    reply="${verdict#*$'\n'}"
    verdict="${verdict%%$'\n'*}"

    case "$verdict" in
        ok) ;;
        empty_reply)
            echo "$label: the turn finalized but the reply was empty, so no output made it back through the plumbing." >&2
            return 1 ;;
        awaiting_approval)
            echo "$label: the turn parked on an approval gate instead of finalizing; the ladder's bundle must not require approval." >&2
            return 1 ;;
        timed_out)
            echo "$label: no reply finalized before the deadline. The worker never completed the turn." >&2
            return 1 ;;
        dry_run)
            echo "$label: got a dry-run descriptor; the ladder must run for real, never with --dry-run." >&2
            return 1 ;;
        not_finalized)
            echo "$label: the payload is neither finalized nor a known terminal shape." >&2
            printf '%s\n' "$payload" >&2
            return 1 ;;
        *)
            echo "$label: could not parse message --json output:" >&2
            printf '%s\n' "$payload" >&2
            return 1 ;;
    esac

    echo "$label: turn finalized with a reply (plumbing asserted, content deliberately not graded)"
    if [[ "$LIVE" == "1" ]]; then
        # Live-only negative control, not a grader: the fake model cannot say
        # anything but the sentinel, so a live run that returns it never reached
        # a real model.
        if [[ "$reply" == "$FAKE_SENTINEL" ]]; then
            echo "$label: live run returned the fake model's canned reply, so the run was not live." >&2
            return 1
        fi
        echo "$label: reply is not the fake sentinel (live negative control)"
    fi
}

# The local reply stub binds a fixed port. A second ladder, or any process
# holding it, would otherwise hang until the 300s message timeout and look like
# a product failure.
assert_stub_port_free() {
    if (exec 3<>"/dev/tcp/127.0.0.1/$STUB_PORT") 2>/dev/null; then
        echo "error: port $STUB_PORT is already in use, and the local reply stub must bind it." >&2
        echo "fix: stop the process holding it (another ladder run, or a stale local message), then re-run." >&2
        return 1
    fi
}

# Rung 1: the existing skill-tier round trip, invoked as is. Never copied.
rung_skill() {
    echo
    echo "########## rung 1/3: skill ##########"
    bash "$REPO_ROOT/cli/scripts/e2e.sh"
}

# Rung 2: the compose tier, cold start to teardown.
rung_local() {
    echo
    echo "########## rung 2/3: local (compose) ##########"

    assert_stub_port_free

    if [[ -n "$(docker ps -q --filter 'name=agentos-api' 2>/dev/null)" ]]; then
        # Reuse it and do NOT tear it down: the thread that brought a stack up
        # owns tearing it down, in both directions.
        echo "a compose stack is already running; reusing it and leaving teardown to whoever started it"
        if [[ "$LIVE" == "1" ]]; then
            # Model mode is fixed at `local up` time, so a reused stack may have
            # been started sealed. Warn rather than refuse: the live-only fake
            # sentinel control below catches it for real.
            echo "warning: AGENTOS_E2E_LIVE=1, but the reused stack's model mode was fixed by whoever ran \`local up\` and is NOT verified here." >&2
            echo "warning: if that stack was started with the fake model, this 'live' rung runs sealed; \`local down\` then re-run to be sure." >&2
        fi
    else
        echo
        echo "=== agentos local up --minimal ==="
        # --minimal selects the core profile and blanks the OTel endpoint itself
        # (core has no collector), so the ladder passes no profiles and no OTel env.
        #
        # Claim ownership BEFORE starting, never after: `local up` blocks for
        # seconds while it waits for health, and containers exist for that whole
        # window. Setting the flag afterwards means a signal or a mid-boot
        # failure leaves the trap disowning a stack this run created, stranding
        # it. Claiming a stack that then fails to boot is harmless, because
        # `local down` is safe against a partial or already-stopped stack.
        LOCAL_STACK_OWNED=1
        "$BIN" local up --minimal
    fi

    echo
    echo "=== agentos local deploy ==="
    # No --api-url: the default IS the cold-start path a real user hits, and
    # exercising the default is the point. First create binds C0LOCALDEV, so the
    # message below can resolve the sole deployed agent with no --channel.
    "$BIN" local deploy --plugin-dir "$WORKDIR/bundle"

    echo
    echo "=== agentos local message --json ==="
    # Re-probe: the precheck above ran before `local up` and `local deploy`,
    # potentially minutes ago, and the stub binds the port only now.
    assert_stub_port_free
    local out
    # `|| true`: the timeout shape exits non-zero, and the assertion helper is
    # what must classify it, not set -e.
    out="$("$BIN" --json local message "$PROMPT" || true)"
    printf '%s\n' "$out"
    assert_finalized_reply "local" "$out"

    if (( LOCAL_STACK_OWNED )); then
        echo
        echo "=== agentos local down ==="
        "$BIN" local down
        LOCAL_STACK_OWNED=0

        echo
        echo "=== assert nothing agentos-related survived ==="
        # Both checks filter by LABEL, never by name. `--filter name=agentos` is
        # a host-wide SUBSTRING match, so on a shared box it reds on containers
        # belonging to other worktrees and sessions (an unrelated
        # `agentos-runner-local` from a concurrent `skill up` is enough), and a
        # gate that cries wolf is a gate someone disables. A run may only assert
        # on what it owns.
        local survivors
        # The compose project name is pinned to `agentos` by the CLI
        # (cli/src/local.rs COMPOSE_PROJECT_NAME), so this selects exactly the
        # services `local up` started and nothing else.
        survivors="$(docker ps --filter 'label=com.docker.compose.project=agentos' --format '{{.Names}}')"
        if [[ -n "$survivors" ]]; then
            echo "local down left compose services running:" >&2
            printf '%s\n' "$survivors" >&2
            return 1
        fi
        # Sandbox containers are named per thread, so a `name=agentos-runner`
        # filter matches nothing and the assertion would pass no matter what
        # survived.
        survivors="$(docker ps --filter "label=$SANDBOX_LABEL" --format '{{.Names}}')"
        if [[ -n "$survivors" ]]; then
            echo "sibling sandbox containers survived teardown:" >&2
            printf '%s\n' "$survivors" >&2
            return 1
        fi
        echo "no agentos containers running"
    fi
}

# Rung 3: the deployed release. Requires one to already exist; it is never
# installed or torn down here, because the cluster is shared.
rung_cluster() {
    echo
    echo "########## rung 3/3: cluster ##########"

    echo
    echo "=== agentos cluster status (gate) ==="
    # Gate on the PAYLOAD, not the exit code: `cluster status` is a read-only
    # report verb and exits 0 even when the release is absent (it just prints
    # "release agentos not found"), so an exit-code gate never fires and the
    # rung falls through into a confusing `cluster deploy` failure instead.
    # --json puts the object on stdout and human text on stderr.
    local status_json found
    status_json="$("$BIN" --json cluster status 2>/dev/null || true)"
    printf '%s\n' "$status_json"
    found="$(printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print("no")
    sys.exit(0)
print("yes" if isinstance(d, dict) and d.get("release_found") is True else "no")
' || echo "no")"
    if [[ "$found" != "yes" ]]; then
        echo "error: AGENTOS_E2E_TIERS named the cluster rung, but no installed release was reported by \`agentos --json cluster status\`." >&2
        echo "fix: install a release with \`agentos cluster up --fake-model\` (or point kubectl at the right context), or drop cluster from AGENTOS_E2E_TIERS." >&2
        return 1
    fi

    echo
    echo "=== agentos cluster deploy ==="
    # No --api-url: deploy auto-discovers the release's UI /api proxy over
    # NodePort. No --secret: it is declined at this tier by design (#440).
    "$BIN" cluster deploy --plugin-dir "$WORKDIR/bundle"

    echo
    echo "=== agentos cluster message --json ==="
    # No --thread: an existing thread keeps the sandbox and bundle it first
    # booted with, so reusing one could silently test a stale bundle. cluster
    # message manages its own port-forwards and reply stub; never forward by hand.
    local out
    out="$("$BIN" --json cluster message "$PROMPT" || true)"
    printf '%s\n' "$out"
    assert_finalized_reply "cluster" "$out"
}

echo
echo "=== ladder configuration ==="
echo "tiers: $TIERS"
apply_model_mode

if [[ "$TIERS" == "all" ]]; then
    TIERS="skill,local,cluster"
fi
RUN_SKILL=0
RUN_LOCAL=0
RUN_CLUSTER=0
IFS=',' read -r -a SELECTED <<< "$TIERS"
for tier in "${SELECTED[@]}"; do
    case "$tier" in
        skill) RUN_SKILL=1 ;;
        local) RUN_LOCAL=1 ;;
        cluster) RUN_CLUSTER=1 ;;
        "") ;;
        *)
            echo "error: unknown tier '$tier' in AGENTOS_E2E_TIERS." >&2
            echo "fix: use a comma list of skill, local, cluster, or the shorthand 'all'." >&2
            exit 1 ;;
    esac
done
if (( ! RUN_SKILL && ! RUN_LOCAL && ! RUN_CLUSTER )); then
    echo "error: AGENTOS_E2E_TIERS selected no rungs." >&2
    echo "fix: set it to a comma list of skill, local, cluster, or 'all'." >&2
    exit 1
fi

# A throwaway COPY of the bundle: deploy records state into the bundle dir, and
# that must never land in the tree.
cp -r "$BUNDLE_SRC" "$WORKDIR/bundle"

# Rungs run strictly in order and never in parallel: they share host ports, and
# rung 1 must release its runner container before rung 2 starts.
if (( RUN_SKILL )); then
    rung_skill
else
    echo
    echo "SKIPPING rung 1 (skill): not named in AGENTOS_E2E_TIERS."
fi
if (( RUN_LOCAL )); then
    rung_local
else
    echo
    echo "SKIPPING rung 2 (local): not named in AGENTOS_E2E_TIERS."
fi
if (( RUN_CLUSTER )); then
    rung_cluster
else
    echo
    echo "SKIPPING rung 3 (cluster): not named in AGENTOS_E2E_TIERS. It needs a live"
    echo "release and host-reachable pods, so it is opt-in: AGENTOS_E2E_TIERS=all."
fi

echo
echo "LADDER PASS (tiers: $TIERS)"
