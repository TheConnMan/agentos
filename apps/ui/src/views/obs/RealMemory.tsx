import { useState } from "react";
import { C } from "../../tokens";
import { Notice } from "../../primitives";
import { useAgents } from "../../api/hooks";
import { WiredAgentMemory } from "../wired/WiredAgentMemory";

// Wired Observability > Memory tab (#869). The GET /agents/{id}/memory endpoint
// is live and already consumed by WiredAgentMemory on the agent detail page, so
// this tab reuses that exact panel rather than a second implementation. Memory
// is per agent, so — like the Cost tab — a selector picks which agent's learned
// memory to inspect/edit/delete; the panel is keyed on the agent id so a switch
// remounts and refetches.
export function RealMemory() {
  const agents = useAgents(true);
  const [selected, setSelected] = useState<string>("");

  const list = agents.data ?? [];
  const activeId = selected || list[0]?.id || "";

  if (agents.loading) return <Notice>Loading agents…</Notice>;
  if (agents.error) return <Notice>Could not load agents: {agents.error}</Notice>;
  if (list.length === 0)
    return <Notice>No agents yet. Deploy an agent to see its learned memory.</Notice>;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <span style={{ fontSize: 12.5, color: C.muted }}>Agent</span>
        <select
          data-testid="memory-agent-select"
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
      <WiredAgentMemory key={`memory-${activeId}`} agentId={activeId} />
    </div>
  );
}
