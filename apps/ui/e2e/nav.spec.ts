import { test, expect } from "@playwright/test";

// Nav works: every sidebar item renders its view at a mature fixture level.
test.describe("navigation", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/?state=6");
  });

  const views: [string, RegExp][] = [
    ["Agents", /Agents/],
    ["Evals", /Run your suite of fixed test cases/],
    ["Observability", /on by default/],
    ["Versions", /main → @agentos/],
    ["Connections", /Connections/],
    ["Settings", /Provider keys/],
    ["Overview", /acme-corp fleet/],
  ];

  for (const [item, marker] of views) {
    test(`nav to ${item}`, async ({ page }) => {
      await page.getByRole("navigation").getByText(item, { exact: true }).click();
      await expect(page.getByText(marker).first()).toBeVisible();
    });
  }
});
