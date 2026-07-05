import { test, expect } from "@playwright/test";

// The magic moment: create-agent modal -> Deploy -> live-in-Slack success panel
// with the Slack evidence card and a toast-free confetti celebration.
test("create-agent deploy flow completes", async ({ page }) => {
  await page.goto("/?state=2");

  await page.getByRole("navigation").getByText("Agents", { exact: true }).click();
  await page.getByRole("button", { name: /New agent/ }).click();

  // The modal shows the template picker + skill.md editor.
  await expect(page.getByText("skills/deal-desk/skill.md").first()).toBeVisible();

  await page.getByRole("button", { name: "Deploy" }).click();
  // 700ms skeleton then the success banner + Slack evidence card.
  await expect(page.getByText(/is live in #revenue-ops/)).toBeVisible();
  await expect(page.getByText("Slack — #revenue-ops")).toBeVisible();
  await expect(page.getByText(/trace tr_8f3k21/)).toBeVisible();
});

test("connect slack flow advances the checklist", async ({ page }) => {
  await page.goto("/?state=1");
  await page.getByRole("navigation").getByText("Connections", { exact: true }).click();
  await page.getByRole("button", { name: "Connect Slack" }).click();
  await page.getByRole("button", { name: "Allow" }).click();
  // Connections now shows a connected Slack row.
  await expect(page.getByText(/Slack · acme-corp.slack.com/)).toBeVisible();
});
