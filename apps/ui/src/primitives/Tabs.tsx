import { C } from "../tokens";

// Underline tab bar used by Observability and Evals. Ported from the inline
// tabBtn() builders in those views.
export function Tabs<T extends string>({
  tabs,
  active,
  onSelect,
}: {
  tabs: readonly (readonly [T, string])[];
  active: T;
  onSelect: (id: T) => void;
}) {
  return (
    <div style={{ display: "flex", borderBottom: "1px solid " + C.border, marginBottom: 22, flexWrap: "wrap" }}>
      {tabs.map(([id, label]) => (
        <button
          key={id}
          type="button"
          onClick={() => onSelect(id)}
          style={{
            padding: "8px 4px",
            background: "none",
            border: "none",
            borderBottom: "2px solid " + (active === id ? C.brand : "transparent"),
            color: active === id ? C.text : C.muted,
            fontSize: 14,
            cursor: "pointer",
            marginRight: 24,
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
