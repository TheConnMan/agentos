import { C } from "../tokens";
import { Button } from "./Button";

// Teaching empty state: possessive title, one sentence, one CTA.
export function EmptyState({
  title,
  sub,
  ctaLabel,
  onCta,
}: {
  title: string;
  sub: string;
  ctaLabel?: string;
  onCta?: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        padding: "80px 20px",
        minHeight: 360,
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
          marginBottom: 16,
        }}
      >
        ○
      </div>
      <h2 style={{ fontSize: 19, fontWeight: 400, color: C.text, margin: "0 0 6px" }}>{title}</h2>
      <p style={{ fontSize: 14, color: C.muted, margin: "0 0 18px", maxWidth: 360 }}>{sub}</p>
      {ctaLabel ? <Button label={ctaLabel} variant="primary" onClick={onCta} /> : null}
    </div>
  );
}
