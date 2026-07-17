// Issue #630 / ADR-0049: the platform key has no browser-reachable path.
//
// These tests pin the negative half of the decision (nothing in the client
// carries the platform key, and the `?api_key=` param is inert rather than
// merely unused) and the positive half (the console-session client calls).
//
// AC map:
//   AC1 "never placed in a URL, static browser bundle, browser storage, or
//        client readable configuration" -> the no-header, param-inert,
//        no-apiKey-export, no-storage tests here.
//   AC3 "authorized without giving browser code the raw platform credential"
//        -> the same-origin credentials tests here.
//   AC2 "revocable authenticated session" -> the getSession/activateSession/
//        logout contract tests here.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, URL as NodeURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as config from "./config";
import * as client from "./client";
import {
  ApiError,
  createAgent,
  createDeployment,
  deleteAgent,
  getConfig,
  listTraces,
  listVersions,
  updateAgent,
} from "./client";

// The console-session contract this suite pins. These are resolved off the
// module namespace rather than imported by name on purpose: `pnpm build` runs
// `tsc -b` over the tests, so a named import of a not-yet-written export would
// fail the type build and take the whole Playwright suite down with it. Resolved
// this way, a missing export is a legible runtime failure in exactly the test
// that needs it, and the real function is exercised once it lands.
export interface ConsoleSession {
  authenticated: boolean;
  expires_at: string | null;
}

function sessionApi<T>(name: string): T {
  const fn = (client as Record<string, unknown>)[name];
  if (typeof fn !== "function") {
    throw new TypeError(`client.${name} is not implemented yet (#630)`);
  }
  return fn as T;
}

const getSession = (): Promise<ConsoleSession> => sessionApi<() => Promise<ConsoleSession>>("getSession")();
const activateSession = (code: string): Promise<ConsoleSession> =>
  sessionApi<(code: string) => Promise<ConsoleSession>>("activateSession")(code);
const logout = (): Promise<void> => sessionApi<() => Promise<void>>("logout")();

// Assembled at runtime so this file itself never contains the literal, which
// would poison the source-scan test below.
const DEV_KEY = ["agentos", "dev", "key"].join("-");
const PLANTED = "supersecret";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function noBody(status: number): Response {
  return new Response(null, { status });
}

type Init = RequestInit & { headers?: Record<string, string> };

function headerNames(init: Init | undefined): string[] {
  return Object.keys(init?.headers ?? {});
}

function headerValues(init: Init | undefined): string[] {
  return Object.values(init?.headers ?? {});
}

// Every call recorded by the fetch spy, flattened to a searchable string so a
// leak anywhere in the URL, headers, or body is caught.
function callText(call: [string, Init | undefined]): string {
  const [url, init] = call;
  return [url, JSON.stringify(init?.headers ?? {}), String(init?.body ?? "")].join(" ");
}

function setSearch(search: string): void {
  window.history.replaceState({}, "", `/${search}`);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setSearch("");
});

// A representative spread of verbs and shapes: GET, POST with a JSON body,
// PATCH, DELETE, and a query-string GET. If any one of them regains the header
// or drops the cookie, one of these fails.
async function exerciseClient(): Promise<void> {
  await createAgent({ name: "deal-desk", slack_channel: "C1" });
  await listVersions("a1");
  await updateAgent("a1", { slack_channel: "C2" });
  await deleteAgent("a1");
  await listTraces(5, "a1");
  await createDeployment({ agent_id: "a1", version_id: "v1", environment: "prod" });
}

describe("the client never carries the platform key (AC1, AC3)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(200, {})));
    vi.stubGlobal("fetch", fetchMock);
  });

  it("sends no X-API-Key header on any verb", async () => {
    await exerciseClient();
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(6);
    for (const [url, init] of fetchMock.mock.calls as [string, Init][]) {
      const names = headerNames(init).map((n) => n.toLowerCase());
      expect(names, `header on ${url}`).not.toContain("x-api-key");
      expect(names, `header on ${url}`).not.toContain("authorization");
    }
  });

  it("sends no header whose value is the published dev key", async () => {
    await exerciseClient();
    for (const [url, init] of fetchMock.mock.calls as [string, Init][]) {
      expect(headerValues(init), `header value on ${url}`).not.toContain(DEV_KEY);
    }
  });

  it("passes credentials same-origin on every authenticated call so the session cookie rides along", async () => {
    await exerciseClient();
    for (const [url, init] of fetchMock.mock.calls as [string, Init][]) {
      expect(init?.credentials, `credentials on ${url}`).toBe("same-origin");
    }
  });

  it("passes credentials same-origin on the open /config call too", async () => {
    await getConfig();
    const [, init] = fetchMock.mock.calls[0] as [string, Init];
    expect(init?.credentials).toBe("same-origin");
  });

  it("leaves ?api_key= inert: it reaches no URL, header, or body", async () => {
    setSearch(`?api=1&api_key=${PLANTED}`);
    await exerciseClient();
    for (const call of fetchMock.mock.calls as [string, Init][]) {
      const text = callText(call);
      expect(text, `leak in call to ${call[0]}`).not.toContain(PLANTED);
      const names = headerNames(call[1]).map((n) => n.toLowerCase());
      expect(names).not.toContain("x-api-key");
    }
  });

  it("makes byte-identical requests with and without ?api_key= present", async () => {
    await exerciseClient();
    const clean = (fetchMock.mock.calls as [string, Init][]).map(callText);

    fetchMock.mockClear();
    setSearch(`?api=1&api_key=${PLANTED}`);
    await exerciseClient();
    const planted = (fetchMock.mock.calls as [string, Init][]).map(callText);

    expect(planted).toEqual(clean);
  });
});

describe("the api module exposes no platform-key surface (AC1)", () => {
  it("exports no apiKey function from config", () => {
    expect(Object.keys(config)).not.toContain("apiKey");
    expect((config as Record<string, unknown>).apiKey).toBeUndefined();
  });

  it("re-exports no apiKey through the client either", () => {
    expect((client as Record<string, unknown>).apiKey).toBeUndefined();
  });

  it("keeps isWired working from ?api=1 and VITE_WIRED", () => {
    setSearch("?api=1");
    expect(config.isWired()).toBe(true);
    setSearch("");
    expect(config.isWired()).toBe(false);
  });

  it("does not contain the dev key string anywhere under src/api", () => {
    const dir = fileURLToPath(new NodeURL(".", import.meta.url));
    const offenders = readdirSync(dir)
      .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
      .filter((f) => readFileSync(join(dir, f), "utf8").includes(DEV_KEY));
    expect(offenders).toEqual([]);
  });
});

describe("console session client (AC2)", () => {
  it("GET /console/session returns the session state", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, { authenticated: true, expires_at: "2026-07-18T00:00:00Z" }));
    vi.stubGlobal("fetch", fetchMock);
    const session = await getSession();
    const [url, init] = fetchMock.mock.calls[0] as [string, Init];
    expect(url).toBe("/api/console/session");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.credentials).toBe("same-origin");
    expect(session.authenticated).toBe(true);
    expect(session.expires_at).toBe("2026-07-18T00:00:00Z");
  });

  it("GET /console/session reports an unauthenticated console rather than throwing on 401", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(401, { detail: "no console session" }));
    vi.stubGlobal("fetch", fetchMock);
    const session = await getSession();
    expect(session.authenticated).toBe(false);
  });

  it("POST /console/session sends the code in the body and returns the live session", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(201, { authenticated: true, expires_at: "2026-07-18T00:00:00Z" }));
    vi.stubGlobal("fetch", fetchMock);
    const session = await activateSession("AAAA-BBBB-CCCC");
    const [url, init] = fetchMock.mock.calls[0] as [string, Init];
    expect(url).toBe("/api/console/session");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    expect(JSON.parse(String(init?.body))).toEqual({ code: "AAAA-BBBB-CCCC" });
    expect(session.authenticated).toBe(true);
  });

  it("POST /console/session surfaces a rejected code as ApiError with the server's fix text", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(400, { detail: "login code is expired or already used" }));
    vi.stubGlobal("fetch", fetchMock);
    const err = (await activateSession("STALE-CODE").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.message).toContain("expired");
  });

  it("DELETE /console/session revokes the session and surfaces failures as ApiError", async () => {
    const ok = vi.fn().mockResolvedValue(noBody(204));
    vi.stubGlobal("fetch", ok);
    await logout();
    const [url, init] = ok.mock.calls[0] as [string, Init];
    expect(url).toBe("/api/console/session");
    expect(init?.method).toBe("DELETE");
    expect(init?.credentials).toBe("same-origin");

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(500, { detail: "store unavailable" })));
    const err = (await logout().catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
  });

  it("never writes the login code to storage or to the URL", async () => {
    // The harness's own setSearch calls history.replaceState, so the URL must be
    // staged BEFORE the spies are installed. Otherwise the spies record the
    // setup's own call and the assertions below could never pass for any
    // implementation. Installed after staging, they observe only activateSession.
    setSearch("?api=1");

    const localSet = vi.spyOn(Storage.prototype, "setItem");
    const push = vi.spyOn(window.history, "pushState");
    const replace = vi.spyOn(window.history, "replaceState");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(201, { authenticated: true, expires_at: "2026-07-18T00:00:00Z" })),
    );

    await activateSession("AAAA-BBBB-CCCC");

    expect(localSet).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain("AAAA-BBBB-CCCC");
  });
});
