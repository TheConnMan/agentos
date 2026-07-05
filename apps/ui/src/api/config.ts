// Backend wiring is resolved at runtime, not baked at build time, so a single
// build serves both the stackless fixture demo and the live integration run.
//
// Resolution:
//  - Wired ON when the URL carries ?api=1, or when built with VITE_WIRED=1.
//  - The API key comes from ?api_key=, else VITE_API_KEY, else the dev default.
//  - Calls always go to the same-origin /api prefix, which Vite proxies to the
//    real API server (apps/api has no CORS, so same-origin is required).
//
// When wired is OFF (the default), every view stays on fixtures exactly as H1a
// shipped it: no network calls, so stackless E2E and unit tests need no backend.

const DEV_API_KEY = "agentos-dev-key";

function params(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}

export function isWired(): boolean {
  if (params().get("api") === "1") return true;
  return import.meta.env.VITE_WIRED === "1";
}

export function apiKey(): string {
  return params().get("api_key") || import.meta.env.VITE_API_KEY || DEV_API_KEY;
}

// Same-origin prefix; Vite's proxy forwards it to AGENTOS_API_TARGET.
export const API_PREFIX = "/api";
