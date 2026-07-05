import { C } from "../tokens";
import { Dot } from "./Dot";
import { useStore } from "../state/store";

// Bottom-right toast bound to store.toast. Auto-dismiss is handled in the store.
export function Toast() {
  const { state } = useStore();
  if (!state.toast) return null;
  return (
    <div
      role="status"
      style={{
        position: "fixed",
        bottom: 24,
        right: 24,
        zIndex: 10000,
        background: C.card,
        border: "1px solid " + C.borderStrong,
        borderRadius: 9,
        padding: "11px 16px",
        display: "flex",
        alignItems: "center",
        gap: 10,
        fontSize: 13.5,
        boxShadow: "0 10px 24px rgba(0,0,0,.4)",
        animation: "fadeUp .25s ease",
      }}
    >
      <Dot color={C.brand} size={7} />
      {state.toast}
    </div>
  );
}
