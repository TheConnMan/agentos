import { useEffect, useRef } from "react";
import { useStore } from "../state/store";

// One-shot confetti burst on first deploy. Canvas animation ported from the
// canon's runConfetti(); clears itself and dispatches confettiDone when finished.
export function Confetti() {
  const { state, dispatch } = useStore();
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    if (!state.confetti) return;
    const cv = ref.current;
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth;
    const hh = cv.clientHeight;
    cv.width = w * dpr;
    cv.height = hh * dpr;
    // getContext throws under jsdom (no canvas backend); treat that as "no
    // confetti" rather than letting it surface as a test-time error.
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = cv.getContext("2d");
    } catch {
      ctx = null;
    }
    if (!ctx) {
      dispatch({ type: "confettiDone" });
      return;
    }
    ctx.scale(dpr, dpr);
    const cols = ["#3ecf8e", "#00c573", "#fafafa", "#BF8700", "#b4b4b4"];
    const N = 120;
    const cx = w * 0.42;
    const cy = hh * 0.32;
    const P = Array.from({ length: N }, () => ({
      x: cx,
      y: cy,
      vx: (Math.random() - 0.5) * 11,
      vy: Math.random() * -11 - 3,
      r: Math.random() * 5 + 2,
      c: cols[(Math.random() * cols.length) | 0],
      rot: Math.random() * 6,
      vr: (Math.random() - 0.5) * 0.4,
    }));
    const t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const el = t - t0;
      ctx.clearRect(0, 0, w, hh);
      P.forEach((p) => {
        p.vy += 0.32;
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.vr;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.globalAlpha = Math.max(0, 1 - el / 1300);
        ctx.fillStyle = p.c;
        ctx.fillRect(-p.r / 2, -p.r / 2, p.r, p.r * 1.6);
        ctx.restore();
      });
      if (el < 1300) raf = requestAnimationFrame(step);
      else dispatch({ type: "confettiDone" });
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [state.confetti, dispatch]);

  if (!state.confetti) return null;
  return (
    <canvas
      ref={ref}
      style={{
        position: "fixed",
        inset: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
        zIndex: 9999,
      }}
    />
  );
}
