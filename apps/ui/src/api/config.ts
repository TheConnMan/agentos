// Backend wiring is resolved at runtime, not baked at build time, so a single
// build serves both the stackless fixture demo and the live integration run.
//
// Resolution:
//  - Wired ON when the URL carries ?api=1, or when built with VITE_WIRED=1.
//  - Calls always go to the same-origin /api prefix, which Vite proxies to the
//    real API server (apps/api has no CORS, so same-origin is required).
//
// There is deliberately no credential here (#630 / ADR-0049). The console
// authenticates with an HttpOnly session cookie, exchanged for a CLI-minted
// single-use login code, so the platform key has no browser-reachable path at
// all: no ?api_key= param, no VITE_API_KEY, no dev-key fallback. Those three
// inputs are deleted rather than deprecated, so there is nothing to regress on.
//
// When wired is OFF (the default), every view stays on fixtures exactly as H1a
// shipped it: no network calls, so stackless E2E and unit tests need no backend.

function params(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}

export function isWired(): boolean {
  if (params().get("api") === "1") return true;
  return import.meta.env.VITE_WIRED === "1";
}

// Same-origin prefix; Vite's proxy forwards it to AGENTOS_API_TARGET.
export const API_PREFIX = "/api";
