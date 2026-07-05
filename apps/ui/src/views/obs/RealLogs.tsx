import { useState } from "react";
import { C } from "../../tokens";
import { Button, Dot } from "../../primitives";
import { getRunnerLogs, ApiError, type PodLogs } from "../../api/client";

type Result =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; data: PodLogs }
  | { kind: "no-cluster"; message: string }
  | { kind: "not-found"; message: string }
  | { kind: "cluster-error"; message: string }
  | { kind: "error"; message: string };

const inputStyle = {
  background: C.input,
  border: "1px solid " + C.borderStrong,
  borderRadius: 8,
  padding: "8px 11px",
  color: C.text,
  fontFamily: C.mono,
  fontSize: 12.5,
} as const;

function StateBanner({ tone, title, body }: { tone: "warn" | "muted" | "error"; title: string; body: string }) {
  const color = tone === "warn" ? C.warn : tone === "error" ? C.failure : C.mutedStatus;
  const bg =
    tone === "warn" ? "rgba(191,135,0,.07)" : tone === "error" ? "rgba(207,34,41,.06)" : "rgba(139,148,158,.06)";
  const border =
    tone === "warn" ? "rgba(191,135,0,.3)" : tone === "error" ? "rgba(207,34,41,.28)" : C.border;
  return (
    <div
      data-testid="logs-state"
      style={{ padding: "18px 20px", background: bg, border: "1px solid " + border, borderRadius: 12, display: "flex", gap: 12, alignItems: "center" }}
    >
      <Dot color={color} size={9} />
      <div>
        <div style={{ fontSize: 14, fontWeight: 500, color: C.text, marginBottom: 2 }}>{title}</div>
        <div style={{ fontSize: 13, color: C.text2, fontFamily: C.mono }}>{body}</div>
      </div>
    </div>
  );
}

// Wired Logs tab: the per-run runner-logs affordance, proxying the K8s pod-logs
// API. Fetch is on demand (namespace + pod), and the distinct cluster states are
// designed: 503 no cluster, 404 pod not found, 502 other cluster error. On the
// dev box (no cluster) the 503 degraded state is the expected result.
export function RealLogs() {
  const [namespace, setNamespace] = useState("agentos");
  const [pod, setPod] = useState("");
  const [result, setResult] = useState<Result>({ kind: "idle" });

  const fetchLogs = async () => {
    if (!pod.trim()) return;
    setResult({ kind: "loading" });
    try {
      const data = await getRunnerLogs(namespace.trim(), pod.trim(), { tail_lines: 200 });
      setResult({ kind: "ok", data });
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 503) setResult({ kind: "no-cluster", message: e.message });
        else if (e.status === 404) setResult({ kind: "not-found", message: e.message });
        else if (e.status === 502) setResult({ kind: "cluster-error", message: e.message });
        else setResult({ kind: "error", message: `${e.status}: ${e.message}` });
      } else {
        setResult({ kind: "error", message: e instanceof Error ? e.message : String(e) });
      }
    }
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
        <input aria-label="namespace" value={namespace} onChange={(e) => setNamespace(e.target.value)} placeholder="namespace" style={{ ...inputStyle, width: 150 }} />
        <input
          data-testid="logs-pod"
          aria-label="pod"
          value={pod}
          onChange={(e) => setPod(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void fetchLogs();
          }}
          placeholder="runner pod name"
          style={{ ...inputStyle, flex: 1, minWidth: 200 }}
        />
        <Button label="Fetch logs" variant="primary" onClick={() => void fetchLogs()} />
      </div>

      {result.kind === "idle" ? (
        <div style={{ padding: "40px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>
          Enter a runner pod to tail its logs. Logs proxy the serving sandbox pod for a run.
        </div>
      ) : null}
      {result.kind === "loading" ? (
        <div style={{ padding: "40px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>Fetching pod logs…</div>
      ) : null}
      {result.kind === "no-cluster" ? (
        <StateBanner tone="warn" title="No cluster configured" body={result.message} />
      ) : null}
      {result.kind === "not-found" ? (
        <StateBanner tone="muted" title="Pod not found" body={result.message} />
      ) : null}
      {result.kind === "cluster-error" ? (
        <StateBanner tone="error" title="Cluster error" body={result.message} />
      ) : null}
      {result.kind === "error" ? <StateBanner tone="error" title="Could not fetch logs" body={result.message} /> : null}
      {result.kind === "ok" ? (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, fontFamily: C.mono, fontSize: 12, color: C.muted }}>
            <span style={{ color: C.text2 }}>
              {result.data.namespace}/{result.data.pod}
              {result.data.container ? ` · ${result.data.container}` : ""}
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6, color: C.brand }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: C.brand }} /> live
            </span>
          </div>
          {result.data.logs.trim() === "" ? (
            <div style={{ padding: "30px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>Pod returned no log lines.</div>
          ) : (
            <pre
              data-testid="logs-output"
              style={{
                margin: 0,
                background: C.darkest,
                border: "1px solid " + C.border,
                borderRadius: 12,
                padding: "12px 14px",
                fontFamily: C.mono,
                fontSize: 12.5,
                lineHeight: 1.6,
                color: C.text2,
                whiteSpace: "pre-wrap",
                maxHeight: 460,
                overflow: "auto",
              }}
            >
              {result.data.logs}
            </pre>
          )}
        </div>
      ) : null}
    </div>
  );
}
