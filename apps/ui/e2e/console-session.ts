import type { Page } from "@playwright/test";

// #630 / ADR-0049: a wired console renders its shell only for an authenticated
// session. The specs that import this exercise the console's *views*, and have
// always implicitly assumed a usable console; this states that precondition
// explicitly now that one exists, the same way they already stub /api/agents.
//
// The gate itself -- unauthenticated, rejected code, fixture mode -- is covered
// by console-login.spec.ts, which drives the real exchange instead of this stub.
export async function stubConsoleSession(page: Page): Promise<void> {
  await page.route("**/api/console/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, expires_at: "2026-07-18T00:00:00Z" }),
    }),
  );
}
