import { C } from "../../tokens";
import { Card, Chip, Dot } from "../../primitives";

// Memory tab: designed coming-soon state. The full memory browser (inspect /
// edit / forget with trace-linked sources) is DEFERRED to a later milestone;
// the ACI memory_ref seam stays in the contract so nothing blocks that build.
export function MemoryStub() {
  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 24,
          background: C.card,
          border: "1px solid " + C.border,
          borderRadius: 14,
          padding: "16px 20px",
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        <div style={{ flex: 1, minWidth: 240 }}>
          <div style={{ fontSize: 14.5, fontWeight: 500, marginBottom: 3, display: "flex", alignItems: "center", gap: 8 }}>
            Managed memory
            <Chip color={C.warn} border="rgba(191,135,0,.4)" pre={<Dot color={C.warn} size={6} />}>
              coming soon
            </Chip>
          </div>
          <div style={{ fontSize: 13, color: C.muted, lineHeight: 1.5, maxWidth: 560 }}>
            AgentOS will write durable facts from live traffic so the agent stops re-deriving them each run, and surface
            every memory here traceable to its source — inspect, edit, or forget.
          </div>
        </div>
      </div>
      <Card>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            textAlign: "center",
            padding: "56px 20px",
            gap: 12,
          }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 10,
              border: "1px solid " + C.borderStrong,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: C.muted,
              fontSize: 20,
            }}
          >
            ⌘
          </div>
          <div style={{ fontSize: 15, color: C.text }}>Memories land here once managed memory ships</div>
          <div style={{ fontSize: 13, color: C.muted, maxWidth: 380 }}>
            Automatic memory generation is not in this release. Traces, metrics, and logs are already on in the other
            tabs.
          </div>
        </div>
      </Card>
    </div>
  );
}
