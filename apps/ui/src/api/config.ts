// The console is always backed by the live API. Calls go to the same-origin
// /api prefix, which Vite proxies to the real API server (apps/api has no CORS,
// so same-origin is required).
//
//  - The API key comes from ?api_key=, else VITE_API_KEY, else the dev default.

const DEV_API_KEY = "agentos-dev-key";

function params(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}

export function apiKey(): string {
  return params().get("api_key") || import.meta.env.VITE_API_KEY || DEV_API_KEY;
}

// Same-origin prefix; Vite's proxy forwards it to AGENTOS_API_TARGET.
export const API_PREFIX = "/api";
