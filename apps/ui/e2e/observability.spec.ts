import { test, expect } from "@playwright/test";

test.describe("observability", () => {
  test("tabs switch and render their surfaces", async ({ page }) => {
    await page.goto("/?state=4");
    await page.getByRole("navigation").getByText("Observability", { exact: true }).click();

    await page.getByRole("button", { name: "Metrics" }).click();
    await expect(page.getByText("Request rate")).toBeVisible();

    await page.getByRole("button", { name: "Logs" }).click();
    await expect(page.getByText("Live tail")).toBeVisible();

    await page.getByRole("button", { name: "Memory" }).click();
    await expect(page.getByText(/coming soon/i)).toBeVisible();

    await page.getByRole("button", { name: "Cost" }).click();
    await expect(page.getByText("Cost / interaction")).toBeVisible();
  });

  test("trace drill-in renders the span tree", async ({ page }) => {
    await page.goto("/?state=4");
    await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
    // open the failed trace
    await page.getByText("tr_2a91cd").click();
    await expect(page.getByText("Slack message")).toBeVisible();
    await expect(page.getByText(/salesforce.get_deal/)).toBeVisible();
    await expect(page.getByText(/hallucinated value/)).toBeVisible();
  });

  test("promote a failed trace into an eval case", async ({ page }) => {
    await page.goto("/?state=4");
    await page.getByRole("navigation").getByText("Observability", { exact: true }).click();
    await page.getByText("tr_2a91cd").click();
    await page.getByRole("button", { name: "Add as eval case" }).click();
    await page.getByRole("button", { name: "Save case" }).click();
    await expect(page.getByText(/Eval suite: 36 → 37 cases/)).toBeVisible();
  });
});
