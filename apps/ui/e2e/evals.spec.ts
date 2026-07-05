import { test, expect } from "@playwright/test";

test.describe("evals", () => {
  test("suite lists cases with pass/fail chips", async ({ page }) => {
    await page.goto("/?state=4");
    await page.getByRole("navigation").getByText("Evals", { exact: true }).click();
    await expect(page.getByText("deal-desk core").first()).toBeVisible();
    await expect(page.getByText("approver-from-policy-source").first()).toBeVisible();
    await expect(page.getByText(/34 of 36 checks passed/)).toBeVisible();
  });

  test("matrix run populates the grid and names the regression", async ({ page }) => {
    await page.goto("/?state=4");
    await page.getByRole("navigation").getByText("Evals", { exact: true }).click();
    await page.getByRole("button", { name: "Matrix" }).click();
    await page.getByRole("button", { name: "Run matrix" }).click();
    await expect(page.getByText(/2 regressions introduced after 4f2c91a/)).toBeVisible();
    // aggregate score headers present
    await expect(page.getByText("97%")).toBeVisible();
    await expect(page.getByText("86%")).toBeVisible();
  });

  test("evals empty state gates on GitHub below level 4", async ({ page }) => {
    await page.goto("/?state=3");
    await page.getByRole("navigation").getByText("Evals", { exact: true }).click();
    await expect(page.getByText("No eval suite yet")).toBeVisible();
    await page.getByRole("button", { name: "Connect GitHub" }).click();
    // now CI is on and the suite renders
    await expect(page.getByText("deal-desk core").first()).toBeVisible();
  });
});
