import type { ReactNode } from "react";

// Centered modal with a dimmed, blurred backdrop. Clicking the backdrop closes;
// clicks inside the panel are stopped. Ported from renderModal().
export function Modal({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9000,
        background: "rgba(0,0,0,.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        backdropFilter: "blur(2px)",
      }}
    >
      <div onClick={(e) => e.stopPropagation()} style={{ animation: "fadeUp .2s ease" }}>
        {children}
      </div>
    </div>
  );
}
