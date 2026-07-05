# apps/worker

Owning tasks: **F1** (the prod-hard concurrency kernel: routing rule, finish-race CAS, steer/interrupt, no-retry-after-side-effects, resume-rehydrate), **G1** (Agent Sandbox substrate module: warm pool, thread-to-sandbox affinity, claim/release), **K1** (eval runner module). F1 is single-owner and never split, with an escalated adversarial review roster. Reads Valkey Streams via redis-py consumer groups; drives claimed sandboxes running the D1 runner image. R0 ships only an empty importable skeleton so the workspace lint and test harness is green.
