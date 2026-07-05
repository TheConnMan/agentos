import { defineConfig, devices } from "@playwright/test";

// Two E2E surfaces share one build (wiring is a runtime ?api=1 flag):
//  - "chromium": stackless fixture E2E (e2e/*.spec.ts). No backend. Always runs.
//  - "integration": live-backend E2E (e2e/integration/*.spec.ts). Needs the
//    compose stack + apps/api. Only added when PW_INTEGRATION=1 so the default
//    run (and CI) stays stackless and green.
//
// The preview proxies /api to AGENTOS_API_TARGET (default the local uvicorn),
// so pointing the integration run at a different API host is one env var.
// Dedicated preview port (away from the 5173 review dev server and any stray
// 4173 preview) so the proxy-enabled preview is always the one under test.
const PORT = Number(process.env.PW_PORT ?? 4273);
const baseURL = process.env.PW_BASE_URL ?? `http://localhost:${PORT}`;
const integration = process.env.PW_INTEGRATION === "1";

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
  projects: [
    {
      name: "chromium",
      testIgnore: "**/integration/**",
      use: { ...devices["Desktop Chrome"] },
    },
    ...(integration
      ? [
          {
            name: "integration",
            testMatch: "**/integration/**/*.spec.ts",
            use: { ...devices["Desktop Chrome"] },
          },
        ]
      : []),
  ],
  webServer: process.env.PW_BASE_URL
    ? undefined
    : {
        command: `pnpm build && pnpm preview --port ${PORT}`,
        url: baseURL,
        // Integration always starts its own proxy-enabled preview (never reuse a
        // stray server); stackless may reuse for speed.
        reuseExistingServer: !process.env.CI && !integration,
        timeout: 120_000,
        env: {
          AGENTOS_API_TARGET: process.env.AGENTOS_API_TARGET ?? "http://localhost:8000",
        },
      },
});
