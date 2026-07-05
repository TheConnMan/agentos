import { defineConfig, devices } from "@playwright/test";

// E2E runs against the built preview server on 4173, or the dev server if
// PW_BASE_URL is set. Fixture-only for H1a: no backend, states driven by ?state=.
const PORT = 4173;
const baseURL = process.env.PW_BASE_URL ?? `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.PW_BASE_URL
    ? undefined
    : {
        command: "pnpm build && pnpm preview",
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
});
