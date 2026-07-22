import { test, expect } from "@playwright/test";

// FX2 item 5: ship a favicon (no console 404).

test("a favicon is declared and served", async ({ page }) => {
  await page.goto("/");
  const icon = page.locator('link[rel="icon"]');
  await expect(icon).toHaveAttribute("href", "/favicon.svg");
  const resp = await page.request.get("/favicon.svg");
  expect(resp.status()).toBe(200);
  expect(resp.headers()["content-type"]).toContain("svg");
});
