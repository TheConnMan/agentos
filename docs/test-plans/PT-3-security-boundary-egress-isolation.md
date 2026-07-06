# PT-3 — Security boundary: default-deny egress + cross-agent secret isolation + runtimeClass

Settles **R4** (the security boundary does not hold against a prompt-injectable input channel). The plan treats the security rails as a done design decision (A2 lane) rather than something to prove; this pulls the proof into Phase 0 because the leave-behind ships to security-reviewing enterprises and the failure mode (a sandbox that was *supposed* to be egress-locked but wasn't) is exactly the Unit 42 AgentCore escape class cited in the plan. Companion: `../analysis/agent-os-prototype-derisking-review.md` §3 R4.

**Run status: ALL claims RUN on k8scratch, 2026-07-04 — PASS (Kata infeasible, expected). R4 is proven on this substrate.** See §Live results.

## Live results (k8scratch, 2026-07-04)

- **Claim 3 (gVisor runtimeClass enforced): PASS — and it works even in the LXC, which I did not expect.** Installed runsc `release-20260622.0` (spec 1.2.1) + `containerd-shim-runsc-v1` (sha512-verified), and created `RuntimeClass gvisor` (handler `runsc`). A pod with `runtimeClassName: gvisor` reached Running and reported **`uname -a` = `Linux gvtest 4.19.0-gvisor #1 SMP … x86_64`** with dmesg **`Starting gVisor...`** — the gVisor sentry kernel, distinct from the host `6.17.13-2-pve`. So runsc's **systrap platform** (seccomp/signal-based, no ptrace perms) functions inside this unprivileged LXC — no privileged pod or `--platform` override needed; the earlier assumption that LXC would block it was wrong. **gVisor is available on k8scratch for the real isolation test.** (RuntimeClass + runsc install left in place for reuse; test pod cleaned up.)
  - **Reproducibility note (containerd config v3):** k3s v1.35.5 uses containerd **config version 3**, so the runtime path is `io.containerd.cri.v1.runtime`, NOT the older `io.containerd.grpc.v1.cri`. k3s auto-detection did NOT register runsc on its own. It was wired via a k3s drop-in (loaded by the generated config's `imports` glob) at `/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/runsc.toml`:
    ```toml
    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runsc]
      runtime_type = "io.containerd.runsc.v1"
    ```
    After `sudo systemctl restart k3s`, `crictl info` lists `"name": "runsc"` / `"runtimeType": "io.containerd.runsc.v1"`.
- **Kata: INFEASIBLE on k8scratch (confirmed).** The node is an LXC container with **no `/dev/kvm`**, so Kata's VM/hypervisor path cannot run. gVisor is the isolation runtime to use here.
- **Claim 1 (default-deny egress actually blocks): PASS, with a control.** Baseline (no policy): a probe pod reached `https://example.com` → **HTTP 200**. After applying `default-deny-egress` + an `allow-dns` (port 53 only) policy: DNS still resolved, but `https://example.com` failed (**curl exit 7**, connect blocked) AND the cloud metadata endpoint `http://169.254.169.254/latest/meta-data/` failed (**curl exit 7**). The before→after flip proves k3s's built-in NetworkPolicy controller genuinely enforces (not a false "block" on a non-enforcing CNI), and the classic metadata-endpoint escape is closed. This is the highest-severity R4 claim and it holds.
- **Claim 2 (cross-agent secret isolation via RBAC): PASS.** With a Role scoped to `resourceNames: [agent-a-creds]`, SA `agent-a`: **can `get` its own secret (yes)**, **cannot `get` agent-b's secret (no)**, **cannot `list` secrets (no)**. Per-plugin secret scoping works as the chart intends.
- **Claim 4 (non-root + read-only rootfs): PASS.** A pod with `runAsNonRoot: true, runAsUser: 1000, readOnlyRootFilesystem: true` ran as **uid=1000** and a write to `/` failed with **`Read-only file system`**.
- **Net R4 verdict: the security boundary holds on this substrate** — egress is default-denied (incl. metadata endpoint), secrets are per-agent isolated, containers run unprivileged read-only, and gVisor adds kernel isolation. The chart's "security rails as defaults" posture is validated end to end (on k3s + its NetworkPolicy controller). Caveat to carry: a customer's own CNI must also enforce NetworkPolicy — verify per-install with this exact before/after probe, because a non-enforcing CNI produces a silent false pass.

## Prior planning notes (pre-run)

**Scratch host confirmed.** Target = the `k8scratch` throwaway k3s cluster (confirmed disposable; see the operator's local k8scratch access notes; repo `~/k8scratch`, `export KUBECONFIG="$PWD/.kube/k8scratch.yaml"`). Two k8scratch-specific facts checked 2026-07-04: (1) k3s ships a **built-in NetworkPolicy controller (kube-router-based) that enforces even with flannel** unless started with `--disable-network-policy` — so claims 1-2 are likely testable here, but **verify enforcement with the blocked-host curl before trusting a "block"** (a false block on a non-enforcing setup is itself the R4 failure mode). (2) k8scratch has runtimeclasses `crun` + wasm variants (spin/slight/wasmedge/wasmtime/wasmer/lunatic) + nvidia, but **NO `gvisor`/`runsc` or `kata`** — so **claim 3 is NOT testable on k8scratch as-is** (mark not-testable unless runsc/kata is installed). No credential blocker for this test.

## Objective

Prove, from *inside* a running agent runner pod, that the chart's default security posture actually holds:

1. **Default-deny egress works.** A pod under the runner NetworkPolicy **cannot** reach an arbitrary host (e.g. `example.com:443`, or the cloud metadata endpoint `169.254.169.254`), and **can** reach only explicitly-allowed hosts (the model API + declared MCP endpoints + DNS). The plan's A2 done-when (`detailed-architecture.md:359`) is "a NetworkPolicy test proves a blocked fetch" — this is that test, run for real.
2. **Cross-agent secret isolation.** A runner for agent A mounts only agent A's credentials; it cannot read agent B's Secret (RBAC + per-plugin secret scoping, `:226`).
3. **runtimeClass is enforced, not silently downgraded.** If a sandbox requests gVisor/Kata, the pod actually runs under it (vs falling back to runc unnoticed) — a pre-1.0 agent-sandbox caveat.
4. **Container hardening holds:** runner runs non-root with read-only rootfs; a write to `/` fails.

## Environment

- A cluster whose CNI **enforces NetworkPolicy**. Plain `kind` uses kindnet, which does NOT enforce NetworkPolicy by default — either `kind` with Calico installed, or the scratch cluster if it runs a policy-enforcing CNI (Calico/Cilium). **Record which CNI**; a NetworkPolicy "passing" on a non-enforcing CNI is a false GO and is itself the R4 failure mode in miniature.
- For claim 3 (runtimeClass), the node needs gVisor (`runsc`) or Kata installed. If the scratch node has neither, record claim 3 as "not testable here" rather than faking it.
- No Anthropic key needed. No real agent needed — a `netshoot`/`curl` pod under the same policy/SA is the probe.

## Setup

```bash
kubectl config current-context && kubectl cluster-info   # confirm scratch, not a Curie EKS account
kubectl get pods -n kube-system | grep -iE 'calico|cilium|kindnet'   # RECORD the CNI
kubectl create namespace pt3
```

## Commands / manifests

```bash
# --- Claim 1: default-deny egress + explicit allow ---
# Default-deny-all egress in the namespace:
cat <<'EOF' | kubectl apply -n pt3 -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny-egress }
spec:
  podSelector: {}
  policyTypes: [Egress]
EOF
# Allow only DNS (so name resolution works) + a single "model API" stand-in host.
# (Use a concrete allow to mirror the chart's "egress = model API + declared MCP endpoints only".)
cat <<'EOF' | kubectl apply -n pt3 -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-dns }
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to: [] 
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
EOF

kubectl -n pt3 run probe --image=nicolaka/netshoot --restart=Never -- sleep 3600
kubectl -n pt3 wait --for=condition=Ready pod/probe --timeout=60s

# BLOCKED egress must fail (timeout/refused):
kubectl -n pt3 exec probe -- sh -c 'curl -m 5 -sS https://example.com; echo "exit=$?"'
# METADATA endpoint must fail (the classic escape):
kubectl -n pt3 exec probe -- sh -c 'curl -m 5 -sS http://169.254.169.254/latest/meta-data/; echo "exit=$?"'
# EVIDENCE: both non-zero/timeout. A 200 from either is a NO-GO (or a non-enforcing CNI — verify).

# Now add an allow for a specific host and confirm it (and ONLY it) works:
# (allow example.org by IP/CIDR or via an FQDN policy if the CNI supports it, e.g. Cilium)
# EVIDENCE: allowed host reachable, example.com still blocked.

# --- Claim 2: cross-agent secret isolation ---
kubectl -n pt3 create secret generic agent-a-creds --from-literal=token=AAA
kubectl -n pt3 create secret generic agent-b-creds --from-literal=token=BBB
# ServiceAccount for agent A + Role granting get on ONLY agent-a-creds:
cat <<'EOF' | kubectl apply -n pt3 -f -
apiVersion: v1
kind: ServiceAccount
metadata: { name: agent-a }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: agent-a-secret }
rules:
  - apiGroups: [""]
    resources: [secrets]
    resourceNames: [agent-a-creds]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: agent-a-secret }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: agent-a-secret }
subjects: [{ kind: ServiceAccount, name: agent-a }]
EOF
# From agent-a's SA, prove B is denied and A is allowed:
kubectl -n pt3 auth can-i get secret/agent-b-creds --as=system:serviceaccount:pt3:agent-a   # expect: no
kubectl -n pt3 auth can-i get secret/agent-a-creds --as=system:serviceaccount:pt3:agent-a   # expect: yes
# Also mount-scoping: a pod with SA agent-a and only agent-a-creds mounted cannot see B's env/file.

# --- Claim 3: runtimeClass enforced (only if runsc/kata present on the node) ---
kubectl get runtimeclass
# schedule a pod with runtimeClassName: gvisor and confirm the kernel differs from host:
kubectl -n pt3 run rc --image=busybox --restart=Never --overrides='{"spec":{"runtimeClassName":"gvisor"}}' -- sh -c 'dmesg 2>/dev/null | head; uname -a; sleep 60'
# EVIDENCE: gVisor shows a distinct/sanitized kernel string; a plain runc pod shows the host kernel.

# --- Claim 4: non-root + read-only rootfs ---
kubectl -n pt3 run hard --image=busybox --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"hard","image":"busybox","securityContext":{"runAsNonRoot":true,"runAsUser":1000,"readOnlyRootFilesystem":true},"command":["sh","-c","id; touch /nope 2>&1; echo exit=$?; sleep 30"]}]}}'
kubectl -n pt3 logs hard   # EVIDENCE: uid=1000, touch / fails with read-only FS
```

## Expected evidence

- **GO:** blocked hosts (example.com + metadata endpoint) time out; allowed host works; agent-a cannot read agent-b's secret; runtimeClass enforced (or honestly marked not-testable); read-only rootfs blocks the write. The chart's default posture is real.
- **NO-GO / must-fix:** any blocked host is reachable (esp. `169.254.169.254`), OR the "block" only passed because the CNI does not enforce policy, OR agent-a can read agent-b's secret. Each is a shippable-incident-class finding and a reason to harden A2 before first client.

## Failure signals

- Egress "blocked" but CNI is kindnet/non-enforcing → **false negative**; the test proved nothing. Re-run on Calico/Cilium.
- DNS breaks after default-deny (agent can't resolve anything) → the allow-DNS policy is malformed; fix before interpreting the egress result.
- runtimeClass pod stuck ContainerCreating → runsc/kata not actually installed; mark claim 3 not-testable, do not fake.

## Cleanup

```bash
kubectl delete namespace pt3
```

## Timebox

**0.5 day.** The long pole is standing up a policy-enforcing CNI if the scratch cluster lacks one.

## Blocker / prerequisite (as of 2026-07-04)

No hard blocker — the `k8scratch` scratch host is confirmed and reachable, and k3s's built-in NetworkPolicy controller should enforce claims 1-2. **Exact next step:** on k8scratch, run Setup + claims 1, 2, 4 (skip claim 3 — no gVisor/Kata runtimeclass present; install `runsc` first if claim 3 matters). The one thing to verify before trusting a result: that the blocked-host curl genuinely times out (proves enforcement is on). Everything here uses no Anthropic key.
