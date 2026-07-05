# apps/ui

Owning tasks: **H1a** (UI shell + design-system port), **H1b** (wired to real backend), **OB1** (Metrics/Logs tabs). The AgentOS console: Vite + React + TypeScript (no meta-framework), design tokens lifted verbatim from `AgentOS.html`'s token block, Playwright for E2E. This directory is reserved at R0 with this README only; **H1a** scaffolds the actual Vite app (its own `package.json`, `pnpm` workspace, and Playwright suite). Until then the CI `ui` job is an echo-skip placeholder.
