import { test, expect, type Page } from "@playwright/test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, URL as NodeURL } from "node:url";

// Issue #630 / ADR-0049: the console authenticates with a server-managed session
// cookie exchanged for a CLI-minted single-use login code. The platform key has
// no browser-reachable path at all.
//
// AC map:
//   AC1 -> the dist scan (no key in the static bundle) and the URL/history proof.
//   AC2 -> the login gate: unauthenticated console renders the login view, and a
//          successful exchange transitions to the real console.
//   AC3 -> the request log carries no X-API-Key header on any request.
//   AC5 -> the dist scan, the history proof, and the request-log proof together.
//
// Stackless: every /api call is stubbed with page.route, so this runs in the
// default `chromium` project with no backend.

// Assembled at runtime so this spec file never contains the literal it scans for.
const DEV_KEY = ["agentos", "dev", "key"].join("-");
const LOGIN_CODE = "AAAA-BBBB-CCCC";

function json(status: number, body: unknown) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}

const SUMMARY = {
  start: "2026-06-28",
  end: "2026-07-05",
  runs: 0,
  latency_p95_ms: 0,
  tokens: 0,
  cost_usd: 0,
  error_rate: 0,
};

// A console-session backend that starts unauthenticated and flips only when the
// right code is exchanged. `sessionCalls` records every method the page used, so
// a test can assert the fixture demo never touches the endpoint at all.
async function stubSession(page: Page) {
  const state = { authenticated: false };
  const sessionCalls: string[] = [];
  const codesSeen: string[] = [];

  await page.route("**/api/console/session", async (route) => {
    const method = route.request().method();
    sessionCalls.push(method);
    if (method === "POST") {
      const body = JSON.parse(route.request().postData() ?? "{}");
      codesSeen.push(body.code);
      if (body.code !== LOGIN_CODE) {
        return route.fulfill(json(400, { detail: "login code is expired or already used" }));
      }
      state.authenticated = true;
      return route.fulfill(json(201, { authenticated: true, expires_at: "2026-07-18T00:00:00Z" }));
    }
    if (method === "DELETE") {
      state.authenticated = false;
      return route.fulfill({ status: 204, body: "" });
    }
    return route.fulfill(
      json(200, {
        authenticated: state.authenticated,
        expires_at: state.authenticated ? "2026-07-18T00:00:00Z" : null,
      }),
    );
  });

  await page.route(/\/api\/agents(\?.*)?$/, (route) => route.fulfill(json(200, [])));
  await page.route("**/api/observability/metrics/summary*", (route) => route.fulfill(json(200, SUMMARY)));
  await page.route("**/api/langfuse/traces*", (route) => route.fulfill(json(200, [])));
  await page.route("**/api/config", (route) => route.fulfill(json(200, { org_name: "acme-corp" })));

  return { sessionCalls, codesSeen };
}

// Record every URL that ever becomes a history entry: the initial one plus every
// pushState/replaceState the app performs. history entries are not enumerable,
// so instrumenting the only two APIs that create them is the complete view.
async function recordHistory(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const seen: string[] = [location.href];
    (window as unknown as { __historyUrls: string[] }).__historyUrls = seen;
    for (const name of ["pushState", "replaceState"] as const) {
      const original = history[name].bind(history);
      history[name] = (...args: Parameters<typeof original>) => {
        original(...args);
        seen.push(location.href);
      };
    }
  });
}

async function historyUrls(page: Page): Promise<string[]> {
  const recorded = await page.evaluate(() => (window as unknown as { __historyUrls: string[] }).__historyUrls);
  return [...recorded, page.url()];
}

async function logIn(page: Page): Promise<void> {
  await page.getByTestId("console-login-code").fill(LOGIN_CODE);
  await page.getByTestId("console-login-submit").click();
}

test("wired + unauthenticated renders the login gate, and a valid code transitions to the real console (AC2)", async ({
  page,
}) => {
  await stubSession(page);
  await page.goto("/?api=1");

  // Gate closed: the login view is up and the console shell is not rendered.
  await expect(page.getByTestId("console-login")).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.getByText("Welcome to AgentOS")).toHaveCount(0);

  await logIn(page);

  // Gate open: a real state transition, not merely an element appearing.
  await expect(page.getByTestId("console-login")).toHaveCount(0);
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("Welcome to AgentOS")).toBeVisible();
});

test("a rejected login code keeps the gate closed and shows the server's reason (AC2)", async ({ page }) => {
  await stubSession(page);
  await page.goto("/?api=1");
  await expect(page.getByTestId("console-login")).toBeVisible();

  await page.getByTestId("console-login-code").fill("STALE-CODE");
  await page.getByTestId("console-login-submit").click();

  await expect(page.getByTestId("console-login-error")).toContainText(/expired|already used/i);
  await expect(page.getByTestId("console-login")).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
});

test("the login code and api_key never enter the URL or any history entry (AC1, AC5)", async ({ page }) => {
  await stubSession(page);
  await recordHistory(page);
  await page.goto("/?api=1");
  await logIn(page);
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 10_000 });

  const urls = await historyUrls(page);
  expect(urls.length).toBeGreaterThan(0);
  for (const url of urls) {
    expect(url, `history entry ${url}`).not.toContain("api_key");
    expect(url, `history entry ${url}`).not.toContain(LOGIN_CODE);
    expect(url, `history entry ${url}`).not.toContain(DEV_KEY);
  }
});

test("a planted ?api_key= is inert: it never reaches a request, and no request carries the key or code (AC3, AC5)", async ({
  page,
}) => {
  await stubSession(page);
  const requests: { url: string; header: string | undefined; kind: string }[] = [];
  page.on("request", (req) =>
    requests.push({ url: req.url(), header: req.headers()["x-api-key"], kind: req.resourceType() }),
  );

  await page.goto(`/?api=1&api_key=${DEV_KEY}`);
  await logIn(page);
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 10_000 });

  // No request the app itself originates may carry the key. The top-level
  // document navigation is excluded because the browser is obliged to fetch the
  // very URL this test plants, and the sibling history test above forbids the
  // app from rewriting it away. Everything the app sends (xhr, fetch, script,
  // stylesheet, image) is in scope, and the document requests are checked
  // separately below.
  const sent = requests.filter((req) => req.kind !== "document");
  expect(sent.length, "the app made no subresource requests: the page never loaded").toBeGreaterThan(0);
  for (const req of sent) {
    expect(req.url, `request to ${req.url}`).not.toContain("api_key=");
    expect(req.url, `request to ${req.url}`).not.toContain(DEV_KEY);
    expect(req.url, `request to ${req.url}`).not.toContain(LOGIN_CODE);
  }

  // Every request, document included, must be header clean.
  for (const req of requests) {
    expect(req.header, `X-API-Key header on ${req.url}`).toBeUndefined();
  }

  // The planted navigation is the one and only document request allowed to
  // carry the key, and it must actually carry it, or this test proves nothing.
  const [planted, ...extraDocs] = requests.filter((req) => req.kind === "document");
  expect(planted?.url, "the api_key param was never planted").toContain(`api_key=${DEV_KEY}`);
  for (const doc of extraDocs) {
    expect(doc.url, `document request to ${doc.url}`).not.toContain("api_key=");
    expect(doc.url, `document request to ${doc.url}`).not.toContain(DEV_KEY);
    expect(doc.url, `document request to ${doc.url}`).not.toContain(LOGIN_CODE);
  }
});

test("fixture mode makes no console-session call and shows no login gate (AC2 scope)", async ({ page }) => {
  const { sessionCalls } = await stubSession(page);
  await page.goto("/?state=3");

  await expect(page.getByTestId("demo-banner")).toBeVisible();
  await expect(page.getByTestId("console-login")).toHaveCount(0);
  await expect(page.getByRole("navigation")).toBeVisible();
  // The stackless fixture demo must stay backend-free.
  expect(sessionCalls).toEqual([]);
});

// AC5's static-asset half. This lives in the Playwright suite because the suite's
// webServer already runs `pnpm build`, so dist/ is guaranteed to be the current
// build. A vitest test would have no such guarantee.
test("the built bundle contains no platform key (AC1, AC5)", async () => {
  const dist = fileURLToPath(new NodeURL("../dist", import.meta.url));

  const files: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) walk(full);
      else files.push(full);
    }
  };
  walk(dist);

  expect(files.length, "dist is empty: the build did not run").toBeGreaterThan(0);
  const offenders = files.filter((f) => readFileSync(f, "utf8").includes(DEV_KEY));
  expect(offenders.map((f) => f.slice(dist.length + 1))).toEqual([]);
});
