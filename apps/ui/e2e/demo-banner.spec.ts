import { test, expect } from "@playwright/test";
import { stubConsoleSession } from "./console-session";

// FX2 item 2: without ?api=1 the UI is a convincing demo on fake data. A
// persistent banner must make that unmistakable and offer the wired app.
test("fixture mode shows the demo-data banner with a connect link", async ({ page }) => {
  await page.goto("/?state=3");
  const banner = page.getByTestId("demo-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Demo data");
  await expect(banner).toContainText("not connected to a backend");
  const link = page.getByTestId("demo-banner-connect");
  await expect(link).toHaveAttribute("href", "?api=1&state=1");
});

test("wired mode does not show the demo banner", async ({ page }) => {
  // #630: sign the console in, so this asserts the wired *shell* has no banner.
  // Without a session the login gate renders instead, and the assertion would
  // pass for the wrong reason -- the banner is absent from the gate too.
  await stubConsoleSession(page);
  // Keep the wired agent list request from hanging the page.
  await page.route("**/api/agents", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.goto("/?api=1");
  await expect(page.getByRole("navigation")).toBeVisible();
  await expect(page.getByTestId("demo-banner")).toHaveCount(0);
});
