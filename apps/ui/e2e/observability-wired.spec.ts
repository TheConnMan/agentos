import { test, expect, type Page } from "@playwright/test";

// Wired Observability (OB1) in the stackless suite: the app runs in ?api=1 mode
// but the observability API is stubbed with real-shaped responses via route
// interception, so these run headless with no backend.

const SUMMARY = {
  start: "2026-06-28T00:00:00Z",
  end: "2026-07-05T00:00:00Z",
  runs: 785,
  // Langfuse reports the latency measure in milliseconds (see lib/format.ts), so
  // 2100 here is 2.1s and must render as "2.10s".
  latency_p95_ms: 2100,
  tokens: 128000,
  cost_usd: 21.4,
  error_rate: 0.018,
};

// Distinct last value per metric so a metric switch is observable in the caption.
const LAST_VALUE: Record<string, number> = {
  runs: 122,
  latency_p95_ms: 3400,
  tokens: 44000,
  cost_usd: 7.25,
  error_rate: 0.06,
};

function seriesFor(metric: string) {
  const last = LAST_VALUE[metric] ?? 100;
  return {
    metric,
    granularity: "day",
    start: SUMMARY.start,
    end: SUMMARY.end,
    points: [
      { ts: "2026-06-30", value: last * 0.8 },
      { ts: "2026-07-01", value: last * 0.9 },
      { ts: "2026-07-02", value: last },
    ],
  };
}

async function stubMetrics(page: Page, seriesBuilder: (metric: string) => object = seriesFor) {
  await page.route("**/api/observability/metrics/summary*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SUMMARY) }),
  );
  await page.route("**/api/observability/metrics/series*", (route) => {
    const metric = new URL(route.request().url()).searchParams.get("metric") ?? "runs";
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seriesBuilder(metric)) });
  });
}

test("Metrics tab renders the summary cards and the series chart from real-shaped data", async ({ page }) => {
  await stubMetrics(page);
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Metrics" }).click();

  const summary = page.getByTestId("metric-summary");
  await expect(summary).toBeVisible();
  await expect(summary).toContainText("785"); // runs
  await expect(summary).toContainText("2.10s"); // latency p95
  await expect(summary).toContainText("$21.40"); // cost
  await expect(summary).toContainText("1.8%"); // error rate (fraction -> percent)
  await expect(page.getByTestId("metric-chart")).toBeVisible();

  // switching the selected metric refetches: the chart's latest value must change
  // to the newly selected metric's series (runs -> 122, error rate -> 6.0%).
  await expect(page.getByTestId("metric-chart-latest")).toHaveText("122");
  await page.getByRole("button", { name: "Error rate" }).click();
  await expect(page.getByTestId("metric-chart-latest")).toHaveText("6.0%");
});

test("a single-point series renders the value without NaN", async ({ page }) => {
  // One-point series (e.g. weekly granularity over a short window) used to divide
  // by zero in the chart; assert it renders the value and no NaN leaks to the DOM.
  await stubMetrics(page, (metric) => ({
    metric,
    granularity: "week",
    start: SUMMARY.start,
    end: SUMMARY.end,
    points: [{ ts: "2026-07-01", value: 137 }],
  }));
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Metrics" }).click();

  await expect(page.getByTestId("metric-chart")).toBeVisible();
  await expect(page.getByTestId("metric-chart-latest")).toHaveText("137");
  const html = await page.content();
  expect(html).not.toContain("NaN");
});

// Stub the runner-pods list endpoint (`/observability/runners`), which populates
// the Logs pod dropdown. A function matcher keeps it from swallowing the per-pod
// `/runners/{ns}/{pod}/logs` route.
async function stubRunnerPods(page: Page, status: number, body: unknown) {
  await page.route(
    (url) => url.pathname.endsWith("/api/observability/runners"),
    (route) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }),
  );
}

test("Logs tab shows the 503 no-cluster degraded state", async ({ page }) => {
  await stubRunnerPods(page, 503, { detail: "no kubernetes cluster configured for runner pods" });
  await page.route("**/api/observability/runners/*/*/logs*", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "no kubernetes cluster configured for runner logs" }),
    }),
  );
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Logs" }).click();

  // The dropdown can't populate without a cluster, so the note flags it.
  await expect(page.getByTestId("pods-note")).toContainText("No cluster configured");
  await page.getByRole("button", { name: "Fetch logs" }).click();

  const stateBanner = page.getByTestId("logs-state");
  await expect(stateBanner).toBeVisible();
  await expect(stateBanner).toContainText("No cluster configured");
  await expect(stateBanner).toContainText("no kubernetes cluster configured");
});

test("Logs tab renders a single pod's logs from the dropdown", async ({ page }) => {
  await stubRunnerPods(page, 200, { namespace: "agentos", pods: ["runner-deal-desk-abc123", "runner-billing-xyz"] });
  await page.route("**/api/observability/runners/*/*/logs*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        namespace: "agentos",
        pod: "runner-deal-desk-abc123",
        container: "runner",
        logs: "14:02:07 info request received\n14:02:08 info verdict routed",
      }),
    }),
  );
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Logs" }).click();
  await page.getByTestId("logs-pod-select").selectOption("runner-deal-desk-abc123");
  await page.getByRole("button", { name: "Fetch logs" }).click();

  await expect(page.getByTestId("logs-output")).toContainText("verdict routed");
});

test("Logs tab aggregates logs across all runner pods by default", async ({ page }) => {
  await stubRunnerPods(page, 200, { namespace: "agentos", pods: ["runner-a", "runner-b"] });
  await page.route("**/api/observability/runners/*/*/logs*", (route) => {
    const pod = new URL(route.request().url()).pathname.split("/").slice(-2)[0];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ namespace: "agentos", pod, container: "runner", logs: `line from ${pod}` }),
    });
  });
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Logs" }).click();
  // The default selection is "All runner pods"; fetch aggregates every pod.
  await page.getByRole("button", { name: "Fetch logs" }).click();

  const out = page.getByTestId("logs-output");
  await expect(out).toContainText("=== runner-a ===");
  await expect(out).toContainText("line from runner-a");
  await expect(out).toContainText("=== runner-b ===");
  await expect(out).toContainText("line from runner-b");
});

test("an agent's View traces opens the Traces list pre-filtered to that agent", async ({ page }) => {
  const agent = { id: "ag-77", name: "billing-bot", slack_channel: "C0BILL", created_at: "2026-07-05T00:00:00Z" };
  await page.route(/\/api\/agents(\?.*)?$/, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([agent]) }));

  const traceUrls: string[] = [];
  await page.route("**/api/langfuse/traces*", (route) => {
    traceUrls.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ id: "t1", name: `agentos-run:agent-${agent.id}-thread-1`, timestamp: "2026-07-05T00:00:00Z" }]),
    });
  });

  await page.goto("/?api=1");
  await page.getByRole("navigation").getByText("Agents", { exact: true }).click();
  await page.getByTestId("view-traces-link").first().click();

  // The traces list opened, filtered: the request carried the agent id token and
  // the filter chip is shown with a clear affordance.
  await expect(page.getByTestId("trace-filter-clear")).toBeVisible();
  await expect.poll(() => traceUrls.some((u) => u.includes(`agent_id=${agent.id}`))).toBe(true);

  // Clearing the filter re-requests without the agent_id.
  await page.getByTestId("trace-filter-clear").click();
  await expect.poll(() => traceUrls.some((u) => u.includes("/langfuse/traces") && !u.includes("agent_id"))).toBe(true);
});

test("Metrics/Logs stay on fixtures without ?api=1", async ({ page }) => {
  await page.goto("/?state=4");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Metrics" }).click();
  // the fixture metrics view shows the Prometheus-style request-rate hero
  await expect(page.getByText("Request rate")).toBeVisible();
  await page.getByRole("button", { name: "Logs" }).click();
  await expect(page.getByText("Live tail")).toBeVisible();
});
