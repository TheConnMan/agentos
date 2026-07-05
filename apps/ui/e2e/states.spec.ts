import { test, expect } from "@playwright/test";

// Each fixture state renders its signature overview surface. This is the
// ?state=N contract Playwright uses in place of the design's demo bar.
const signatures: [number, RegExp][] = [
  [1, /Welcome to AgentOS/],
  [2, /Welcome to AgentOS/],
  [3, /agent live/],
  [4, /agents? live/],
  [5, /agents? live/],
  [6, /acme-corp fleet/],
];

for (const [state, marker] of signatures) {
  test(`state ${state} overview renders`, async ({ page }) => {
    await page.goto(`/?state=${state}`);
    await expect(page.getByText(marker).first()).toBeVisible();
  });
}

test("state 2 checks the Connect Slack step", async ({ page }) => {
  await page.goto("/?state=2");
  // Slack step is done (struck through), Create-agent is next with a Start CTA.
  await expect(page.getByRole("button", { name: "Start" })).toBeVisible();
});

test("dev switcher only appears with ?dev=1", async ({ page }) => {
  await page.goto("/?state=6");
  await expect(page.getByTestId("state-switcher")).toHaveCount(0);
  await page.goto("/?state=6&dev=1");
  await expect(page.getByTestId("state-switcher")).toBeVisible();
  await page.getByTestId("switch-state-4").click();
  await expect(page.getByText(/agents? live/).first()).toBeVisible();
});
