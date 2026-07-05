import { test, expect, type Page } from "@playwright/test";

// Wired Observability (OB1) in the stackless suite: the app runs in ?api=1 mode
// but the observability API is stubbed with real-shaped responses via route
// interception, so these run headless with no backend.

const SUMMARY = {
  start: "2026-06-28T00:00:00Z",
  end: "2026-07-05T00:00:00Z",
  runs: 785,
  latency_p95_seconds: 2.1,
  tokens: 128000,
  cost_usd: 21.4,
  error_rate: 0.018,
};

function seriesFor(metric: string) {
  return {
    metric,
    granularity: "day",
    start: SUMMARY.start,
    end: SUMMARY.end,
    points: [
      { ts: "2026-06-28", value: 90 },
      { ts: "2026-06-29", value: 110 },
      { ts: "2026-06-30", value: 105 },
      { ts: "2026-07-01", value: 130 },
      { ts: "2026-07-02", value: 128 },
      { ts: "2026-07-03", value: 140 },
      { ts: "2026-07-04", value: 122 },
    ],
  };
}

async function stubMetrics(page: Page) {
  await page.route("**/api/observability/metrics/summary*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SUMMARY) }),
  );
  await page.route("**/api/observability/metrics/series*", (route) => {
    const metric = new URL(route.request().url()).searchParams.get("metric") ?? "runs";
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(seriesFor(metric)) });
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

  // switching the selected metric refetches and keeps the chart rendered
  await page.getByRole("button", { name: "Error rate" }).click();
  await expect(page.getByTestId("metric-chart")).toBeVisible();
});

test("Logs tab shows the 503 no-cluster degraded state", async ({ page }) => {
  await page.route("**/api/observability/runners/**/logs*", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "no kubernetes cluster configured for runner logs" }),
    }),
  );
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
  await page.getByRole("button", { name: "Logs" }).click();

  await page.getByTestId("logs-pod").fill("runner-deal-desk-abc123");
  await page.getByRole("button", { name: "Fetch logs" }).click();

  const stateBanner = page.getByTestId("logs-state");
  await expect(stateBanner).toBeVisible();
  await expect(stateBanner).toContainText("No cluster configured");
  await expect(stateBanner).toContainText("no kubernetes cluster configured");
});

test("Logs tab renders pod logs on success", async ({ page }) => {
  await page.route("**/api/observability/runners/**/logs*", (route) =>
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
  await page.getByTestId("logs-pod").fill("runner-deal-desk-abc123");
  await page.getByRole("button", { name: "Fetch logs" }).click();

  await expect(page.getByTestId("logs-output")).toContainText("verdict routed");
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
