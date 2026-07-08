import { useEffect, useState } from "react";
import { C } from "../../tokens";
import { Card, Button, AreaChart, Chip, Notice } from "../../primitives";
import { useAgents, useCost } from "../../api/hooks";
import {
  getBudget,
  putBudget,
  getKillState,
  killAgent,
  resumeAgent,
  ApiError,
  type BudgetConfig,
} from "../../api/client";

const numInput = {
  width: 160,
  background: C.input,
  border: "1px solid " + C.borderStrong,
  borderRadius: 7,
  padding: "8px 10px",
  color: C.text,
  fontFamily: C.mono,
  fontSize: 13,
} as const;

// ---- Cost panel: total + daily spend chart, honest empty state ----
function CostPanel({ agentId }: { agentId: string }) {
  const { data, loading, error } = useCost(agentId);
  return (
    <Card style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>Cost</div>
      {loading ? (
        <Notice>Loading cost…</Notice>
      ) : error ? (
        <Notice>Could not load cost: {error}</Notice>
      ) : !data ? null : data.total_usd === 0 ? (
        <div>
          <div style={{ fontFamily: C.mono, fontSize: 28, color: C.text, marginBottom: 4 }} data-testid="cost-total">
            $0.00
          </div>
          <Notice>No spend recorded in this window yet.</Notice>
        </div>
      ) : (
        <div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 10 }}>
            <span style={{ fontFamily: C.mono, fontSize: 28, color: C.text }} data-testid="cost-total">
              ${data.total_usd.toFixed(2)}
            </span>
            <span style={{ fontSize: 12.5, color: C.muted }}>total · {data.points.length} days</span>
          </div>
          <div data-testid="cost-chart">
            <AreaChart data={data.points.map((p) => p.value)} color={C.brand} height={110} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---- Budget panel: display + edit, client-side positive validation, 422 surfaced ----
function BudgetPanel({ agentId }: { agentId: string }) {
  const [config, setConfig] = useState<BudgetConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [usd, setUsd] = useState("");
  const [tokens, setTokens] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    setEditing(false);
    getBudget(agentId)
      .then((c) => {
        if (!live) return;
        setConfig(c);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(String(e));
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [agentId]);

  const beginEdit = () => {
    setUsd(config?.max_usd_per_day != null ? String(config.max_usd_per_day) : "");
    setTokens(config?.max_output_tokens_per_run != null ? String(config.max_output_tokens_per_run) : "");
    setFormError(null);
    setEditing(true);
  };

  const parseField = (raw: string, integer: boolean): number | null | "invalid" => {
    const t = raw.trim();
    if (t === "") return null; // empty -> platform default
    // Number() (not parseFloat) rejects the whole string: parseFloat("1,000") is
    // 1 and parseFloat("25usd") is 25, which would silently truncate the cap.
    const n = Number(t);
    if (!Number.isFinite(n) || n <= 0 || (integer && !Number.isInteger(n))) return "invalid";
    return n;
  };

  const save = async () => {
    const u = parseField(usd, false);
    const tk = parseField(tokens, true);
    if (u === "invalid") return setFormError("Max $/day must be a positive number.");
    if (tk === "invalid") return setFormError("Max tokens/run must be a positive whole number.");
    setFormError(null);
    setSaving(true);
    try {
      const saved = await putBudget(agentId, { max_usd_per_day: u, max_output_tokens_per_run: tk });
      setConfig(saved);
      setEditing(false);
    } catch (e) {
      setFormError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const shown = (v: number | null | undefined, prefix: string) =>
    v == null ? "platform default" : prefix + v;

  return (
    <Card style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div style={{ fontSize: 14, fontWeight: 500 }}>Budget</div>
        {!loading && !error && !editing ? (
          <div style={{ marginLeft: "auto" }}>
            <Button label="Edit" size="sm" onClick={beginEdit} />
          </div>
        ) : null}
      </div>
      {loading ? (
        <Notice>Loading budget…</Notice>
      ) : error ? (
        <Notice>Could not load budget: {error}</Notice>
      ) : editing ? (
        <div data-testid="budget-form">
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 12 }}>
            <label style={{ fontSize: 12.5, color: C.muted }}>
              <div style={{ marginBottom: 5 }}>Max $/day</div>
              <input data-testid="budget-usd" value={usd} onChange={(e) => setUsd(e.target.value)} placeholder="platform default" style={numInput} />
            </label>
            <label style={{ fontSize: 12.5, color: C.muted }}>
              <div style={{ marginBottom: 5 }}>Max output tokens/run</div>
              <input data-testid="budget-tokens" value={tokens} onChange={(e) => setTokens(e.target.value)} placeholder="platform default" style={numInput} />
            </label>
          </div>
          {formError ? (
            <div data-testid="budget-error" style={{ fontSize: 12.5, color: C.destructive, marginBottom: 12, fontFamily: C.mono }}>
              {formError}
            </div>
          ) : null}
          <div style={{ fontSize: 11.5, color: C.muted, marginBottom: 12 }}>Leave a field blank for the platform default.</div>
          <div style={{ display: "flex", gap: 10 }}>
            <Button label="Cancel" variant="ghost" size="sm" onClick={() => setEditing(false)} />
            {saving ? <Button label="Saving…" size="sm" disabled /> : <Button label="Save budget" variant="primary" size="sm" onClick={() => void save()} />}
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 34, flexWrap: "wrap" }} data-testid="budget-display">
          <div>
            <div style={{ fontSize: 11.5, color: C.muted, marginBottom: 3 }}>Max $/day</div>
            <div style={{ fontFamily: C.mono, fontSize: 15 }}>{shown(config?.max_usd_per_day, "$")}</div>
          </div>
          <div>
            <div style={{ fontSize: 11.5, color: C.muted, marginBottom: 3 }}>Max output tokens/run</div>
            <div style={{ fontFamily: C.mono, fontSize: 15 }}>{shown(config?.max_output_tokens_per_run, "")}</div>
          </div>
        </div>
      )}
    </Card>
  );
}

// ---- Kill switch: the emergency stop, with confirm-before-kill ----
function KillPanel({ agentId }: { agentId: string }) {
  const [killed, setKilled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    setConfirming(false);
    getKillState(agentId)
      .then((s) => {
        if (!live) return;
        setKilled(s.killed);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(String(e));
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [agentId]);

  const doKill = async () => {
    setBusy(true);
    try {
      const s = await killAgent(agentId);
      setKilled(s.killed);
      setConfirming(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const doResume = async () => {
    setBusy(true);
    try {
      const s = await resumeAgent(agentId);
      setKilled(s.killed);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <Card>
        <Notice>Loading kill state…</Notice>
      </Card>
    );
  }
  if (error) {
    return (
      <Card>
        <Notice>Could not load kill state: {error}</Notice>
      </Card>
    );
  }

  if (killed) {
    // Unmistakable killed treatment: a red, filled emergency banner.
    return (
      <div
        data-testid="kill-panel"
        data-killed="true"
        style={{
          background: "rgba(207,34,41,.12)",
          border: "1px solid " + C.failure,
          borderRadius: 14,
          padding: "18px 20px",
          display: "flex",
          alignItems: "center",
          gap: 16,
        }}
      >
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: 10,
            background: "rgba(207,34,41,.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: C.failure,
            fontSize: 20,
            flexShrink: 0,
          }}
        >
          ■
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: C.failure, letterSpacing: 0.3 }} data-testid="kill-status">
            AGENT KILLED
          </div>
          <div style={{ fontSize: 13, color: C.text2 }}>This agent is stopped and not serving requests. Resume to bring it back online.</div>
        </div>
        {busy ? <Button label="Resuming…" disabled /> : <Button label="Resume agent" variant="primary" onClick={() => void doResume()} />}
      </div>
    );
  }

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }} data-testid="kill-panel" data-killed="false">
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 2, display: "flex", alignItems: "center", gap: 8 }}>
            Kill switch
            <Chip color={C.success} border="rgba(46,160,67,.4)">
              live
            </Chip>
          </div>
          <div style={{ fontSize: 13, color: C.text2 }} data-testid="kill-status">
            Emergency stop. Halts the agent immediately across all channels. Use if it is misbehaving or overspending.
          </div>
        </div>
        {confirming ? (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 12.5, color: C.destructive, fontWeight: 500 }}>Stop the agent now?</span>
            <Button label="Cancel" variant="ghost" size="sm" onClick={() => setConfirming(false)} />
            {busy ? (
              <Button label="Killing…" variant="danger" size="sm" disabled />
            ) : (
              <Button label="Confirm kill" variant="danger" size="sm" onClick={() => void doKill()} />
            )}
          </div>
        ) : (
          <Button label="Kill agent" variant="danger" onClick={() => setConfirming(true)} />
        )}
      </div>
    </Card>
  );
}

// Wired Cost view (L1): per-agent cost + budget + kill switch. Agent-scoped
// because the L1 endpoints are per agent; a selector picks which agent. Keeps
// the canon Cost aesthetic (dark cards, mono figures) rather than the fixture
// fleet table.
export function RealCost() {
  const agents = useAgents(true);
  const [selected, setSelected] = useState<string>("");

  const list = agents.data ?? [];
  const activeId = selected || list[0]?.id || "";

  if (agents.loading) return <Notice>Loading agents…</Notice>;
  if (agents.error) return <Notice>Could not load agents: {agents.error}</Notice>;
  if (list.length === 0) return <Notice>No agents yet. Deploy an agent to see its cost and budget.</Notice>;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <span style={{ fontSize: 12.5, color: C.muted }}>Agent</span>
        <select
          data-testid="cost-agent-select"
          value={activeId}
          onChange={(e) => setSelected(e.target.value)}
          style={{
            background: C.input,
            border: "1px solid " + C.borderStrong,
            borderRadius: 7,
            padding: "7px 10px",
            color: C.text,
            fontFamily: C.mono,
            fontSize: 13,
          }}
        >
          {list.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <CostPanel key={`cost-${activeId}`} agentId={activeId} />
      <BudgetPanel key={`budget-${activeId}`} agentId={activeId} />
      <KillPanel key={`kill-${activeId}`} agentId={activeId} />
    </div>
  );
}
