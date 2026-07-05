import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

// Live-backend E2E for H1b. Requires: the compose dev stack, apps/api uvicorn,
// and AGENTOS_API_TARGET pointing at it (the preview proxies /api there). Run:
//   PW_INTEGRATION=1 pnpm exec playwright test --project=integration

const API = process.env.AGENTOS_API_TARGET ?? "http://localhost:8000";
const API_KEY = process.env.AGENTOS_API_KEY ?? "agentos-dev-key";
const REPO_ROOT = resolve(process.cwd(), "../..");

const api = (path: string, init?: RequestInit) =>
  fetch(`${API}${path}`, { ...init, headers: { "X-API-Key": API_KEY, ...(init?.headers ?? {}) } });

// Seed one OTLP trace and wait until it surfaces (with observations) through the
// API proxy, so the Runs assertions have a real span tree to render.
async function seedTrace(): Promise<string> {
  const out = execFileSync("uv", ["run", "python", "apps/ui/e2e/integration/seed_trace.py"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  const traceId = out.trim().split("\n").pop()!.trim();
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const resp = await api(`/langfuse/traces/${traceId}`);
    if (resp.status === 200) {
      const body = await resp.json();
      if (Array.isArray(body.tree) && body.tree.length > 0) return traceId;
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error(`seeded trace ${traceId} never surfaced through the proxy`);
}

test.beforeAll(async () => {
  // Ensure a real trace exists and has propagated through the proxy before the
  // Runs assertions read it. The UI matches it by name (h1b-ui-wire-demo).
  await seedTrace();
});

test("create agent -> Deploy -> version + stored bundle exist via API", async ({ page }) => {
  const agentName = `dealdesk-${Date.now()}`;
  await page.goto("/?state=2&api=1");

  await page.getByRole("navigation").getByText("Agents", { exact: true }).click();
  await page.getByRole("button", { name: /New agent/ }).click();
  await page.getByTestId("agent-name").fill(agentName);
  await page.getByTestId("agent-channel").fill("#support");
  await page.getByRole("button", { name: "Deploy" }).click();

  // Honest post-deploy panel proves the whole chain (create agent + version +
  // bundle) ran and names the real next step.
  const panel = page.getByTestId("deployed-panel");
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect(panel).toContainText(agentName);
  await expect(panel).toContainText("#support");

  // Verify server-side: the agent, its version, and a stored bundle all exist.
  const agents = await (await api("/agents")).json();
  const agent = agents.find((a: { name: string }) => a.name === agentName);
  expect(agent, "created agent is listed by the API").toBeTruthy();

  const versions = await (await api(`/agents/${agent.id}/versions`)).json();
  expect(versions.length).toBeGreaterThan(0);
  const version = versions[0];
  expect(version.bundle_ref, "version has a stored bundle_ref").toBeTruthy();
  expect(version.bundle_sha256, "version has a bundle sha256").toBeTruthy();

  const bundleResp = await api(`/agents/${agent.id}/versions/${version.id}/bundle`);
  expect(bundleResp.status, "bundle bytes are fetchable").toBe(200);
  const bytes = await bundleResp.arrayBuffer();
  expect(bytes.byteLength).toBeGreaterThan(0);
});

test("a malformed skill.md surfaces the validator error inline", async ({ page }) => {
  const agentName = `broken-${Date.now()}`;
  await page.goto("/?state=2&api=1");

  await page.getByRole("navigation").getByText("Agents", { exact: true }).click();
  await page.getByRole("button", { name: /New agent/ }).click();
  await page.getByTestId("agent-name").fill(agentName);
  await page.getByTestId("agent-channel").fill("C0MALFORMED");
  // No YAML frontmatter -> the plugin_format validator rejects the SKILL.md.
  await page.getByTestId("skill-editor").fill("this skill has no frontmatter at all");
  await page.getByRole("button", { name: "Deploy" }).click();

  const errors = page.getByTestId("deploy-errors");
  await expect(errors).toBeVisible({ timeout: 15_000 });
  await expect(errors).toContainText(/skill|frontmatter|manifest/i);
  // The modal stays open and the (new) success panel never appears on failure.
  await expect(page.getByTestId("deployed-panel")).toHaveCount(0);
});

test("Runs tab lists the seeded trace and drill-in renders its span tree", async ({ page }) => {
  await page.goto("/?state=3&api=1");
  await page.getByRole("navigation").getByText("Observability", { exact: true }).click();

  // The live traces list shows real Langfuse traces.
  const row = page.getByTestId("trace-row").filter({ hasText: "h1b-ui-wire-demo" }).first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  await row.click();

  // Drill-in reconstructs the observation tree; the model span maps to GENERATION.
  const tree = page.getByTestId("span-tree");
  await expect(tree).toBeVisible();
  await expect(tree).toContainText("GENERATION");
  await expect(tree).toContainText(/execute_tool|salesforce|slack/i);
});
