# PT-1 — Agent Sandbox lifecycle + control-endpoint reachability

Settles risk **R1** (Agent Sandbox does not deliver the interactive control endpoint the ACI assumes) and touches R6 (footprint). This is the top-ranked prototype: the entire interactive path (DAG lanes D1/F1/G1) hangs on the outcome. Companion: `../analysis/agent-os-prototype-derisking-review.md` §3 R1.

**Run status: RUN on the `k8scratch` throwaway k3s cluster, 2026-07-04. Verdict: GO-with-one-material-caveat.** Live evidence in §Live results below; offline schema findings in §Evidence-so-far. The routable-endpoint assumption is confirmed live; the suspend/resume-is-cold-restart risk is confirmed real. Cluster was cleaned up after (disposable-default).

## Live results (k8scratch, k3s v1.35.5+k3s1, 2026-07-04)

Ran on the confirmed-throwaway `k8scratch` single-node k3s cluster (context gated: `current-context == k8scratch`, server `k8scratch:6443`, NOT an EKS Curie account). Controller = agent-sandbox v0.5.0 core `manifest.yaml`.

- **Controller install: CLEAN.** `agent-sandbox-controller` rolled out `1/1 Running`, zero restarts, no cert-manager/webhook dependency errors, no registered validating/mutating webhooks. A good stability data point for a pre-1.0 project on a stock k3s.
- **Claim 1 (Sandbox lifecycle): PASS.** `apiVersion: agents.x-k8s.io/v1beta1`, `kind: Sandbox` created; its managed pod reached `1/1 Running` in <8s; `.status.podIPs` populated; `.status.conditions` showed `Ready / DependenciesReady`.
- **Claim 2 (routable endpoint): PASS — but the Service is OPT-IN via `spec.service: true`.** With `spec.service` unset, `.status.serviceFQDN`/`.status.service` stayed empty and NO Service was created. With `spec.service: true`, `.status.serviceFQDN = <name>.<ns>.svc.cluster.local`, `.status.service = <name>`, and a **headless ClusterIP Service** was auto-created. A probe pod reached `http://<serviceFQDN>:<port>/` and got the expected body (`exit=0`). **So the worker CAN dial a claimed sandbox at `.status.serviceFQDN` — the core R1 routing assumption holds — provided the platform sets `spec.service: true` on every interactive sandbox.**
- **Claim 4 (hibernate/resume): CONFIRMED COLD RESTART — the material caveat.** `operatingMode: Suspended` **deleted the managed pod** (observed pod-exists 1→0 within ~6s). `operatingMode: Running` (resume) created a **brand-new pod: different `metadata.uid` (`eeb0d23d…`→`b07328fb…`), new `startTime`, new `containerStartedAt`.** The `serviceFQDN` stayed stable and re-bound to the fresh pod, so routing identity is durable — but the **live in-RAM process does NOT survive suspend/resume.** With `shutdownPolicy: Retain` only a PVC-backed volume would persist; the emptyDir scratch used here was lost with the pod. **Consequences: (a) R1 — "steering into the live run" only works within a single un-suspended claim; across a suspend the session must rehydrate from stored history. (b) R2 — prompt-cache warmth cannot survive a suspend/resume cycle (the process is gone and the 5-min TTL is moot). The budget/cost model must not assume cross-hibernation cache warmth.**
- **Claim 3 (SandboxWarmPool): NOT RUN — extensions not installed.** Confirmed the core `manifest.yaml` ships only the `Sandbox` CRD; `SandboxWarmPool`/`SandboxClaim`/`SandboxTemplate` need the separate extensions manifest, which was not applied in this timebox. This remains the open sub-item: the warm-pool allocation path (`detailed-architecture.md:151-172`) is unverified and is an additional pre-1.0 surface. **Next step:** locate the v0.5.0 extensions asset (or build from repo `config/`), apply, and run the warm-pool + claim-latency steps below.

**Net:** GO on Agent Sandbox as the interactive substrate for routing/identity; the interactive design MUST treat every resume as a cold rehydrate (statelessness-as-floor, which the plan already states at `detailed-architecture.md:34`) and MUST NOT bank on cache warmth across hibernation. Warm-pool remains to be proven.

## Evidence so far (offline, no cluster — 2026-07-04)

## Evidence so far (offline, no cluster — 2026-07-04)

Pulled `https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.5.0/manifest.yaml` (396 KB) and parsed the CRDs:

- **The core manifest ships ONLY the `Sandbox` CRD** (`sandboxes.agents.x-k8s.io`, group `agents.x-k8s.io`, served versions `v1beta1` and `v1alpha1`). `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool` are **NOT** in it — they are a separate extensions install to locate and apply (claim 3 below now has a prerequisite: find the extensions manifest).
- **Claim 2 (routable endpoint) is CONFIRMED at schema level.** `Sandbox.status` properties: **`service`, `serviceFQDN`**, `podIPs`, `nodeName`, `conditions`, `selector`. The worker's dial target is `.status.serviceFQDN`.
- **Claim 4 (hibernation) surface identified.** `Sandbox.spec` has **`operatingMode` (enum `Running`|`Suspended`)**, **`shutdownPolicy` (enum `Delete`|`Retain`)**, **`shutdownTime`**, `service`, `podTemplate`, `volumeClaimTemplates`. Hypothesis to confirm on-cluster: setting `operatingMode: Suspended` then back to `Running` **restarts the process** (cold resume); `shutdownPolicy: Retain` preserves the scratch volume but not the live process. The v1alpha1 version additionally has `replicas` in spec/status.
- **apiVersion correction for the manifests below:** use `agents.x-k8s.io/v1beta1` (served), not the `v1alpha1` guess originally written.

## Objective

Prove or disprove, with captured output, four claims the AgentOS build plan assumes about `kubernetes-sigs/agent-sandbox` v0.5.0:

1. The controller installs cleanly and a `Sandbox` reaches Running on the target cluster.
2. A claimed/running sandbox exposes a **stable, routable endpoint** (Service or stable DNS in `.status`) the worker could dial while the pod is Running.
3. A **`SandboxWarmPool`** pre-warms pods and a claim binds to a warm one (near-instant allocation, the plan's `:151-172` assumption).
4. **Hibernate → resume** behavior: does resume preserve a live in-pod process/socket, or is it a pod/process restart (cold rehydrate)? This is the make-or-break sub-question for both R1 (steering) and R2 (cache warmth).

The deliverable is a GO / NO-GO / PARTIAL memo on whether Agent Sandbox is the interactive substrate, or whether the plan must fall back to plain-K8s-Jobs + a self-managed runner pool for the interactive path.

## Environment

- **Target: the throwaway K8s scratch host** identified in the scratch-host connection notes (see Blocker — target must be confirmed as throwaway test infra before any `kubectl apply`).
- **Fallback if no scratch host / not throwaway: local `kind`** (`kind create cluster`). `kind` is NOT currently installed on this box (`kind: MISSING` per tooling check) — `go install sigs.k8s.io/kind@latest` or the release binary first. `kubectl` (`/snap/bin/kubectl`) and `helm` (`/usr/local/bin/helm`) are present. `docker` is present.
- gVisor/Kata runtime classes are NOT required for PT-1 (that is PT-3); PT-1 can run with the default runtime to test lifecycle/routing.

## Setup

```bash
# 1. Confirm the target is the scratch cluster, NOT a Curie env. Print server + context and eyeball it.
kubectl config current-context
kubectl cluster-info
# HARD GATE: if the server host resolves to *.eks.amazonaws.com in account REDACTED-AWS-ACCT (ei-agents/staging)
# or REDACTED-AWS-ACCT (prod), STOP — that is a shared Curie cluster, not the scratch host.

# 2. Install the agent-sandbox controller + CRDs (v0.5.0, the version verified 2026-07-04).
VERSION=v0.5.0
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/manifest.yaml
kubectl get crd | grep -i sandbox   # expect: sandboxes, sandboxtemplates, sandboxclaims, sandboxwarmpools
kubectl -n agent-sandbox-system get deploy,pod   # controller healthy
```

## Commands / manifests

```bash
# A namespace for the experiment so cleanup is a single delete.
kubectl create namespace pt1

# --- Claim 1 & 2: a single Sandbox, then inspect its status for a routable endpoint ---
cat <<'EOF' | kubectl apply -n pt1 -f -
apiVersion: agents.x-k8s.io/v1beta1   # confirmed served version at v0.5.0 (schema inspection 2026-07-04)
kind: Sandbox
metadata:
  name: pt1-solo
spec:
  operatingMode: Running
  shutdownPolicy: Retain
  podTemplate:
    spec:
      containers:
        - name: agent
          image: python:3.13-slim
          command: ["sleep", "3600"]
EOF

kubectl -n pt1 get sandbox pt1-solo -o yaml | tee /tmp/pt1-solo-status.yaml
# EVIDENCE TO CAPTURE: .status.service and .status.serviceFQDN are the documented endpoint fields (confirmed in schema).
kubectl -n pt1 get sandbox pt1-solo -o jsonpath='{.status.serviceFQDN}{"\n"}{.status.service}{"\n"}{.status.podIPs}{"\n"}'
kubectl -n pt1 get svc,endpoints    # confirm the Service named in status actually exists and has endpoints

# Reachability test: exec a second debug pod and curl/nc the sandbox's advertised address.
kubectl -n pt1 run probe --image=nicolaka/netshoot --restart=Never -- sleep 3600
# once the sandbox exposes an addr (from status), from probe:
kubectl -n pt1 exec probe -- sh -c 'nc -zv <sandbox-addr> <port>; getent hosts <sandbox-fqdn>'

# --- Claim 3: SandboxWarmPool (EXTENSIONS install — NOT in the core manifest) ---
# First locate + apply the extensions manifest (SandboxTemplate/Claim/WarmPool live here, not in manifest.yaml).
# Check the release assets / repo `config/` for an extensions manifest, e.g.:
#   kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.5.0/extensions.yaml
# (confirm the exact asset name on the v0.5.0 release page; if absent, build from the repo `config/` kustomize).
kubectl get crd | grep -iE 'sandboxtemplate|sandboxclaim|sandboxwarmpool'   # must appear before proceeding
cat <<'EOF' | kubectl apply -n pt1 -f -
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxWarmPool
metadata:
  name: pt1-pool
spec:
  replicas: 2
  template:            # confirm field name via `kubectl explain sandboxwarmpool.spec`
    spec:
      podTemplate:
        spec:
          containers:
            - name: agent
              image: python:3.13-slim
              command: ["sleep", "3600"]
EOF
kubectl -n pt1 get pods -w   # observe pre-warmed pods appear Ready before any claim
# Then create a SandboxClaim and time how fast it binds:
cat <<'EOF' | kubectl apply -n pt1 -f -
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxClaim
metadata:
  name: pt1-claim
spec:
  templateRef: { name: pt1-pool }   # confirm claim->pool/template wiring via explain
EOF
kubectl -n pt1 get sandboxclaim pt1-claim -o yaml   # bound? to a warm pod? latency?

# --- Claim 4: hibernate / resume ---
# Write a marker into the running sandbox, then trigger hibernation (TTL/idle or explicit),
# then resume and check whether the PROCESS survived (marker in /proc) vs only the FILESYSTEM survived.
kubectl -n pt1 exec pt1-solo -- sh -c 'echo $$ > /data/pid.marker; echo hello > /data/fs.marker; date > /data/started.at'
# trigger hibernation: set operatingMode to Suspended (confirmed enum in v1beta1 schema):
kubectl -n pt1 patch sandbox pt1-solo --type merge -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n pt1 get sandbox pt1-solo -o jsonpath='{.status.conditions}'   # wait for Suspended
# resume:
kubectl -n pt1 patch sandbox pt1-solo --type merge -p '{"spec":{"operatingMode":"Running"}}'
# then:
kubectl -n pt1 exec pt1-solo -- sh -c 'cat /data/fs.marker; cat /data/started.at; ps aux | head; cat /data/pid.marker; echo "current pid1 start:"; stat -c %Y /proc/1'
# INTERPRETATION: fs.marker present + NEW process start time / pid1 restarted => resume is a COLD process restart
# (scratch survives, live process does NOT). fs.marker present + SAME start time => live process survived (best case).
```

## Expected evidence

- **GO:** all four CRDs served; sandbox reaches Running; `.status` (or an auto-created Service) gives a routable addr that `probe` can reach; warm pool pre-warms and a claim binds sub-second; resume preserves the live process (`/proc/1` start time unchanged).
- **PARTIAL (most likely):** lifecycle + routing + warm pool all work, but **resume is a cold process restart** (filesystem scratch persists, live process does not). This is still usable but forces the design to rehydrate session-from-history on resume and concedes R2 (cache warmth) across hibernation — record it loudly.
- **NO-GO:** no routable endpoint in status / no per-sandbox Service (worker cannot dial the harness); or warm pool unimplemented at v0.5.0; or controller unstable. Any of these pushes the interactive path to the plain-K8s-Job fallback.

## Failure signals

- CRD apiVersion/group differs from the manifest above (v0.5.0 may serve a different group) → `kubectl explain` and adjust; not a real failure, a doc-drift.
- Controller CrashLoopBackOff → check RBAC/webhook cert; a pre-1.0 controller instability data point for R1's blast-radius note.
- Sandbox stuck Pending → describe for scheduling/PVC issues (scratch storage class must support the CRD's volume claims).
- `probe` cannot reach the sandbox addr → the routing assumption (B1) is unproven; escalate to NO-GO.

## Cleanup

```bash
kubectl delete namespace pt1
kubectl delete -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.5.0/manifest.yaml
# if kind fallback:
kind delete cluster
```
On the scratch cluster, `kubectl delete namespace pt1` removes all experiment objects; the controller uninstall is optional if the cluster is truly throwaway but do it to leave no CRDs behind.

## Timebox

**0.5 day.** If the controller is not healthy and CRDs not served within the first hour, stop and record the pre-1.0 instability as the finding — that itself is a material R1 data point.

## Blocker (as of 2026-07-04)

Target cluster not yet confirmed. This plan must run on the **throwaway K8s scratch host** named in Brian's connection notes; those notes had not been located/confirmed at authoring time (a search subagent was dispatched — see the review's cover message). **Exact next step:** confirm the scratch host kubeconfig/context, verify `kubectl cluster-info` does NOT point at an `*.eks.amazonaws.com` Curie account (REDACTED-AWS-ACCT / REDACTED-AWS-ACCT), then run Setup. If no throwaway host exists, install `kind` locally and run there — every step except the specific storage-class/runtimeClass behavior is identical.
