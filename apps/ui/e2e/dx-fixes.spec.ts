import { test, expect } from "@playwright/test";

// FX2 item 5: ship a favicon (no console 404) and label the CLI view as a demo
// mock so its canned version/model strings do not read as live.

test("a favicon is declared and served", async ({ page }) => {
  await page.goto("/?state=3");
  const icon = page.locator('link[rel="icon"]');
  await expect(icon).toHaveAttribute("href", "/favicon.svg");
  const resp = await page.request.get("/favicon.svg");
  expect(resp.status()).toBe(200);
  expect(resp.headers()["content-type"]).toContain("svg");
});

test("the CLI view is labeled as a demo mock", async ({ page }) => {
  await page.goto("/?state=3");
  await page.getByRole("button", { name: /CLI view/ }).click();
  await expect(page.getByText("Demo mock")).toBeVisible();
  await expect(page.getByTestId("cli-demo-note")).toContainText("not the live CLI");
});
