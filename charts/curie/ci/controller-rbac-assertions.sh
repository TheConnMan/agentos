#!/usr/bin/env bash
#
# Render-assertion test for issue #350 (controller NetworkPolicy RBAC:
# cluster-read + namespaced-mutate split, plus the install-time controller-ready
# gate). Pins the verb-split shape so re-vendoring the upstream agent-sandbox
# controller -- or any RBAC edit -- cannot silently regress in EITHER direction:
# re-widening mutate to cluster scope (regresses #66) or re-breaking the
# informer by confining cluster LIST/WATCH to a namespaced Role (the #350
# crash-loop). See docs/adr/0023-controller-networkpolicy-rbac-cluster-read-namespace-mutate.md.
#
# Six assertions. (a)-(e) scan the FULL multi-doc render (ClusterRoles come from
# BOTH templates/agent-sandbox.yaml and the vendored
# files/agent-sandbox/controller.yaml, so no --show-only); (f) EXECUTES the
# rendered preflight script against a stub kubectl:
#
#   (a) Exactly ONE cluster-scope ClusterRole grants networkpolicies, its verb
#       set is exactly {get,list,watch}, bound to SA agent-sandbox-controller in
#       agent-sandbox-system. (informer can sync; read-only)
#   (b) NO ClusterRole grants any mutate verb on networkpolicies anywhere
#       (the #66 regression tripwire; passes today, guards future re-vendoring).
#   (c) A namespaced Role agent-sandbox-controller-networkpolicies in the release
#       namespace keeps create/delete/patch/update/get and drops list/watch,
#       bound to the same SA.
#   (d) The controller-ready preflight gate renders with defaults and suppresses
#       correctly under agentSandbox.controller.deploy=false and
#       preflights.controllerReady.enabled=false.
#   (e) The gate's FAIL diagnostic has a lease-specific branch (issue #507).
#   (f) The gate's classifier BEHAVES: run the rendered script under sh with a
#       stub kubectl serving crafted logs. It must not fabricate an RBAC match
#       from two concatenated logs, and cause-specific remediation must print
#       only under its own branch (issue #611). (e) is presence-only and cannot
#       see either bug.
#
# Runnable locally (from anywhere) and from CI. Fails loudly, naming the
# violated assertion.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

# Deterministic release name + namespace so the release-namespace assertion (c)
# is unambiguous. curie.fullname collapses to the release name here (the chart
# name "curie" is a substring of "curie-assert").
RELEASE="curie-assert"
NS="curie-assert"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

DEFAULT="$TMP/default.yaml"
NOCTRL="$TMP/noctrl.yaml"
NOGATE="$TMP/nogate.yaml"

echo "=== Rendering chart (defaults) ==="
helm template "$RELEASE" "$CHART" --namespace "$NS" > "$DEFAULT"

echo "=== Rendering chart (agentSandbox.controller.deploy=false) ==="
helm template "$RELEASE" "$CHART" --namespace "$NS" \
  --set agentSandbox.controller.deploy=false > "$NOCTRL"

echo "=== Rendering chart (preflights.controllerReady.enabled=false) ==="
helm template "$RELEASE" "$CHART" --namespace "$NS" \
  --set preflights.controllerReady.enabled=false > "$NOGATE"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# All four assertions run in one PyYAML pass over the three renders. The Python
# emits an "ok:" line per assertion and exits nonzero (printing the reason) on
# the first violation, which the bash fail() then surfaces by assertion label.
ASSERT_PY="$TMP/assert.py"
cat > "$ASSERT_PY" <<'PY'
import sys, yaml

default_path, noctrl_path, nogate_path, release_ns = sys.argv[1:5]

CONTROLLER_SA = ("agent-sandbox-controller", "agent-sandbox-system")
NP_ROLE = "agent-sandbox-controller-networkpolicies"
READ_SUFFIX = "networkpolicies-read"
PREFLIGHT_SUFFIX = "-preflight-controller"
MUTATE_VERBS = {"create", "delete", "patch", "update", "deletecollection", "*"}


def load(path):
    with open(path) as f:
        return [d for d in yaml.safe_load_all(f) if d]


def docs_of_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def rule_is_about_networkpolicies(rule):
    # A rule may list several resources/apiGroups in one entry; treat it as
    # touching networkpolicies if "networkpolicies" (or the "*" wildcard) is in
    # its resources.
    resources = rule.get("resources") or []
    return "networkpolicies" in resources or "*" in resources


def networkpolicy_verbs(role):
    verbs = set()
    for rule in role.get("rules") or []:
        if rule_is_about_networkpolicies(rule):
            verbs.update(rule.get("verbs") or [])
    return verbs


def role_mentions_networkpolicies(role):
    return any(rule_is_about_networkpolicies(r) for r in (role.get("rules") or []))


def binding_targets(binding, role_kind, role_name):
    ref = binding.get("roleRef") or {}
    if ref.get("kind") != role_kind or ref.get("name") != role_name:
        return False
    for subj in binding.get("subjects") or []:
        if (
            subj.get("kind") == "ServiceAccount"
            and (subj.get("name"), subj.get("namespace")) == CONTROLLER_SA
        ):
            return True
    return False


def die(msg):
    sys.stdout.write(msg + "\n")
    sys.exit(1)


default_docs = load(default_path)
noctrl_docs = load(noctrl_path)
nogate_docs = load(nogate_path)

cluster_roles = docs_of_kind(default_docs, "ClusterRole")
cluster_role_bindings = docs_of_kind(default_docs, "ClusterRoleBinding")

# --- (a) Exactly one cluster-scope read grant, nothing more ---
np_cluster_roles = [cr for cr in cluster_roles if role_mentions_networkpolicies(cr)]
if len(np_cluster_roles) != 1:
    names = sorted((cr.get("metadata") or {}).get("name") for cr in np_cluster_roles)
    die(
        "(a) exactly one cluster read grant — expected exactly 1 ClusterRole "
        "with a networkpolicies rule, found %d: %s" % (len(np_cluster_roles), names)
    )
read_cr = np_cluster_roles[0]
read_cr_name = (read_cr.get("metadata") or {}).get("name")
verbs = networkpolicy_verbs(read_cr)
if verbs != {"get", "list", "watch"}:
    die(
        "(a) exactly one cluster read grant — ClusterRole %r networkpolicies "
        "verbs must be exactly {get, list, watch}, got %s"
        % (read_cr_name, sorted(verbs))
    )
if not any(binding_targets(b, "ClusterRole", read_cr_name) for b in cluster_role_bindings):
    die(
        "(a) exactly one cluster read grant — no ClusterRoleBinding binds "
        "ClusterRole %r to ServiceAccount agent-sandbox-controller in "
        "agent-sandbox-system" % read_cr_name
    )
print("  ok: (a) exactly one networkpolicies ClusterRole %r, verbs {get,list,watch}, bound to the controller SA" % read_cr_name)

# --- (b) No cluster-wide mutate, anywhere ---
for cr in cluster_roles:
    name = (cr.get("metadata") or {}).get("name")
    bad = networkpolicy_verbs(cr) & MUTATE_VERBS
    if bad:
        die(
            "(b) no cluster-wide mutate — ClusterRole %r grants mutate verb(s) "
            "%s on networkpolicies (regresses #66)" % (name, sorted(bad))
        )
print("  ok: (b) no ClusterRole grants create/delete/patch/update/* on networkpolicies")

# --- (c) Namespaced mutate intact ---
np_roles = [
    r
    for r in docs_of_kind(default_docs, "Role")
    if (r.get("metadata") or {}).get("name") == NP_ROLE
]
if len(np_roles) != 1:
    die("(c) namespaced mutate intact — expected exactly one Role %r, found %d" % (NP_ROLE, len(np_roles)))
np_role = np_roles[0]
role_ns = (np_role.get("metadata") or {}).get("namespace")
if role_ns != release_ns:
    die("(c) namespaced mutate intact — Role %r must be in the release namespace %r, got %r" % (NP_ROLE, release_ns, role_ns))
rverbs = networkpolicy_verbs(np_role)
required = {"create", "delete", "patch", "update", "get"}
missing = required - rverbs
if missing:
    die("(c) namespaced mutate intact — Role %r networkpolicies verbs must be a superset of %s, missing %s" % (NP_ROLE, sorted(required), sorted(missing)))
forbidden = {"list", "watch"} & rverbs
if forbidden:
    die("(c) namespaced mutate intact — Role %r must NOT grant %s on networkpolicies (now served cluster-wide, #350)" % (NP_ROLE, sorted(forbidden)))
role_bindings = [
    rb
    for rb in docs_of_kind(default_docs, "RoleBinding")
    if (rb.get("metadata") or {}).get("namespace") == release_ns
]
if not any(binding_targets(rb, "Role", NP_ROLE) for rb in role_bindings):
    die("(c) namespaced mutate intact — no RoleBinding in %r binds Role %r to the controller SA" % (release_ns, NP_ROLE))
print("  ok: (c) namespaced Role %r keeps create/delete/patch/update/get, drops list/watch, bound to the controller SA" % NP_ROLE)

# --- (d) Gate renders/suppresses with its flags ---
def names_by_kind(docs, kind):
    return [(d.get("metadata") or {}).get("name") for d in docs_of_kind(docs, kind)]

def has_suffix(names, suffix):
    return [n for n in names if n and n.endswith(suffix)]

# (d.1) defaults: preflight Job + its ServiceAccount render.
default_jobs = has_suffix(names_by_kind(default_docs, "Job"), PREFLIGHT_SUFFIX)
if not default_jobs:
    die("(d) gate renders — defaults must render a Job whose name ends with %r; none found" % PREFLIGHT_SUFFIX)
default_sas = has_suffix(names_by_kind(default_docs, "ServiceAccount"), PREFLIGHT_SUFFIX)
if not default_sas:
    die("(d) gate renders — defaults must render a ServiceAccount whose name ends with %r; none found" % PREFLIGHT_SUFFIX)
print("  ok: (d.1) defaults render preflight Job %s and its ServiceAccount" % default_jobs)

# (d.2) controller.deploy=false: NONE of the gate Job, the read ClusterRole/CRB,
# the namespaced Role/RoleBinding render.
noctrl_offenders = []
noctrl_offenders += ["Job " + n for n in has_suffix(names_by_kind(noctrl_docs, "Job"), PREFLIGHT_SUFFIX)]
noctrl_offenders += ["ClusterRole " + n for n in has_suffix(names_by_kind(noctrl_docs, "ClusterRole"), READ_SUFFIX)]
noctrl_offenders += [
    "ClusterRoleBinding " + n
    for n in has_suffix(names_by_kind(noctrl_docs, "ClusterRoleBinding"), READ_SUFFIX)
]
noctrl_offenders += ["Role " + n for n in names_by_kind(noctrl_docs, "Role") if n == NP_ROLE]
noctrl_offenders += ["RoleBinding " + n for n in names_by_kind(noctrl_docs, "RoleBinding") if n == NP_ROLE]
if noctrl_offenders:
    die("(d) gate suppresses — with controller.deploy=false these must NOT render: %s" % noctrl_offenders)
print("  ok: (d.2) controller.deploy=false suppresses the gate Job, the read ClusterRole/CRB, and the namespaced Role/RoleBinding")

# (d.3) controllerReady.enabled=false (deploy still true): Job absent, but the
# read ClusterRole and namespaced Role STILL render (RBAC split is independent).
nogate_jobs = has_suffix(names_by_kind(nogate_docs, "Job"), PREFLIGHT_SUFFIX)
if nogate_jobs:
    die("(d) gate suppresses — with controllerReady.enabled=false the preflight Job must be absent, found %s" % nogate_jobs)
nogate_read = has_suffix(names_by_kind(nogate_docs, "ClusterRole"), READ_SUFFIX)
if not nogate_read:
    die("(d) RBAC split independent of gate — with controllerReady.enabled=false the read ClusterRole (*%s) must still render" % READ_SUFFIX)
nogate_role = [n for n in names_by_kind(nogate_docs, "Role") if n == NP_ROLE]
if not nogate_role:
    die("(d) RBAC split independent of gate — with controllerReady.enabled=false Role %r must still render" % NP_ROLE)
print("  ok: (d.3) controllerReady.enabled=false suppresses only the Job; the RBAC split still renders")
PY

if ! out="$(python3 "$ASSERT_PY" "$DEFAULT" "$NOCTRL" "$NOGATE" "$NS" 2>&1)"; then
  fail "$out"
fi
echo "$out"

# --- (e) The preflight FAIL diagnostic classifies the CAUSE (issue #507) ---
# A leader-election lease timeout and the #350 RBAC crash-loop both restart the
# controller pod, so the gate must not blame NetworkPolicy RBAC unconditionally.
# Assert the rendered Job script both DETECTS the lease signal and emits a
# lease-specific (non-RBAC) diagnostic, so a regression back to the hardcoded
# RBAC blame fails here.
grep -q "leader election lost\|failed to renew lease" "$DEFAULT" \
  || fail "(e) cause classification — preflight script must grep for a leader-election lease signal (issue #507)"
grep -q "lost its leader-election lease" "$DEFAULT" \
  || fail "(e) cause classification — preflight FAIL diagnostic must have a lease-specific branch (issue #507)"
grep -q "NOT an RBAC/NetworkPolicy problem" "$DEFAULT" \
  || fail "(e) cause classification — the lease diagnostic must explicitly disclaim RBAC as the cause (issue #507)"
echo "  ok: (e) the controller-ready gate distinguishes a lease timeout from an RBAC failure"

# --- (f) The classifier BEHAVES correctly, executed against a stub kubectl ---
# (e) above is presence-only: it greps the render for strings and so cannot see
# either a log-concatenation false match or a diagnostic leaking across case
# branches. So actually RUN the rendered script under `sh` with a stub kubectl
# that serves crafted logs per scenario, and assert on its stdout (issue #611).
FDIR="$TMP/f"
mkdir -p "$FDIR/bin"

# Pull the inline /bin/sh -c script body out of the preflight Job's container.
python3 - "$DEFAULT" "$FDIR/preflight.sh" <<'PY' || fail "(f) behavioral classifier -- could not extract the preflight script from the render"
import sys, yaml

render_path, out_path = sys.argv[1:3]

with open(render_path) as f:
    docs = [d for d in yaml.safe_load_all(f) if d]

jobs = [
    d
    for d in docs
    if d.get("kind") == "Job"
    and ((d.get("metadata") or {}).get("name") or "").endswith("-preflight-controller")
]
if len(jobs) != 1:
    sys.stdout.write("expected exactly one *-preflight-controller Job, found %d\n" % len(jobs))
    sys.exit(1)

containers = jobs[0]["spec"]["template"]["spec"]["containers"]
command = containers[0].get("command") or []
if len(command) < 3 or command[0] != "/bin/sh" or command[1] != "-c":
    sys.stdout.write("preflight container command is not the expected /bin/sh -c form: %r\n" % (command,))
    sys.exit(1)

with open(out_path, "w") as f:
    f.write(command[2])
PY

# Stub kubectl. Dispatches on its args and serves the logs for ${SCENARIO}; the
# --previous case must be matched before the plain logs case (the real call adds
# --previous to an otherwise identical argv).
cat > "$FDIR/bin/kubectl" <<'STUB'
#!/bin/sh
case "$*" in
  *rollout*status*)
    exit 0
    ;;
  *get*pods*jsonpath*)
    # Nonzero restartCount, so the script fetches the --previous log at all.
    printf '1'
    ;;
  *get*pods*)
    printf 'agent-sandbox-controller-0   0/1   Error   1   30s\n'
    ;;
  *logs*--previous*)
    if [ "${SCENARIO}" = "lease_glue" ]; then
      # FIRST line is a leases-forbidden line: glued onto the current log's
      # networkpolicies-mentioning last line it fabricates an RBAC signature.
      printf 'Error: leases.coordination.k8s.io "agent-sandbox-controller" is forbidden: User cannot get resource\n'
      printf 'E0717 12:00:01 failed to renew lease agent-sandbox-system/agent-sandbox-controller: context deadline exceeded\n'
    fi
    ;;
  *logs*)
    if [ "${SCENARIO}" = "lease_glue" ]; then
      printf 'I0717 12:00:00 starting manager\n'
      # LAST line mentions networkpolicies but carries no "forbidden".
      printf 'I0717 12:00:00 reflector starting for networkpolicies\n'
    else
      printf 'I0717 12:00:00 starting manager\n'
      printf 'E0717 12:00:00 reflector: failed to list *v1.NetworkPolicy: networkpolicies.networking.k8s.io is forbidden: User "system:serviceaccount:agent-sandbox-system:agent-sandbox-controller" cannot list resource "networkpolicies" at the cluster scope\n'
    fi
    ;;
esac
exit 0
STUB
chmod +x "$FDIR/bin/kubectl"

# TIMEOUT is small so the poll loop cannot linger; every scenario breaks out on
# the first iteration anyway. The FAIL path is expected to exit 1, so the
# function itself must not propagate that under set -e (it would abort this
# script) -- it stashes the exit code in $FDIR/rc for the caller to assert on
# instead.
run_preflight() {
  PATH="$FDIR/bin:$PATH" SCENARIO="$1" \
    CONTROLLER_NS=agent-sandbox-system DEPLOY=agent-sandbox-controller TIMEOUT=5 \
    sh "$FDIR/preflight.sh" 2>&1
  echo $? > "$FDIR/rc"
}

# (f.1) The glue regression (AC1): a lease failure whose current log also
# mentions networkpolicies must classify as lease, not rbac.
lease_out="$(run_preflight lease_glue)"
lease_rc="$(cat "$FDIR/rc")"
[ "$lease_rc" -eq 1 ] \
  || fail "(f.1) exit code -- the lease FAIL path must exit 1 (ADR-0023's gate must not pass a broken controller), got $lease_rc:
$lease_out"
echo "$lease_out" | grep -q "lost its leader-election lease" \
  || fail "(f.1) classifier glue -- a lease failure whose logs also mention networkpolicies must classify as lease, got:
$lease_out"
echo "$lease_out" | grep -q "forbidden networkpolicies log" \
  && fail "(f.1) classifier glue -- concatenating the current and previous logs fabricated an RBAC match (issue #611), got:
$lease_out"
echo "  ok: (f.1) a lease failure whose logs mention networkpolicies classifies as lease, not rbac, and exits 1"

# (f.2) Branch leak (AC2/AC3): the crash-loop hint is written for the #350 RBAC
# signature and must not trail a lease diagnostic. Presence-only greps cannot
# catch this; only running the lease branch can. Anchored on the hint's
# distinctive prose ("delete the controller") rather than "CrashLoopBackOff",
# since that status also appears in the FAIL diagnostic's own `kubectl get
# pods` dump and would false-FAIL the moment the stub's stubbed pod status
# stops being "Error".
echo "$lease_out" | grep -q "NOT an RBAC/NetworkPolicy problem" \
  || fail "(f.2) branch leak -- the lease diagnostic must disclaim RBAC, got:
$lease_out"
echo "$lease_out" | grep -q "delete the controller" \
  && fail "(f.2) branch leak -- the RBAC crash-loop hint must NOT print under the lease branch (issue #611), got:
$lease_out"
echo "  ok: (f.2) the lease branch emits no RBAC crash-loop hint"

# (f.3) The RBAC path still works: a genuine forbidden-networkpolicies line
# classifies as rbac AND keeps its crash-loop remediation.
rbac_out="$(run_preflight rbac)"
rbac_rc="$(cat "$FDIR/rc")"
[ "$rbac_rc" -eq 1 ] \
  || fail "(f.3) exit code -- the rbac FAIL path must exit 1 (ADR-0023's gate must not pass a broken controller), got $rbac_rc:
$rbac_out"
echo "$rbac_out" | grep -q "forbidden-networkpolicies logged" \
  || fail "(f.3) rbac path -- a genuine forbidden-networkpolicies log must classify as rbac, got:
$rbac_out"
echo "$rbac_out" | grep -q "delete the controller" \
  || fail "(f.3) rbac path -- the rbac branch must keep its crash-loop remediation, got:
$rbac_out"
echo "  ok: (f.3) a genuine RBAC failure classifies as rbac, keeps its crash-loop hint, and exits 1"

echo
echo "PASS: exactly one read-only cluster networkpolicies grant (get/list/watch, bound to the controller SA); no cluster-wide mutate anywhere; namespaced Role keeps mutate and drops list/watch; the controller-ready gate renders on defaults, suppresses correctly under both flags, classifies a lease timeout distinctly from an RBAC failure, and -- executed against a stub kubectl -- neither fabricates an RBAC match from two concatenated logs nor leaks the RBAC crash-loop hint into the lease branch."
