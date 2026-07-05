# tests/soak

Owning task: **N1**. The soak/chaos suite that proves the definition-of-done: concurrent threads + a mid-thread batch job + sandbox-kill-mid-run + resume-rehydrate, asserting no cross-talk, no duplicate side effects, and sandbox-affined `cache_read_input_tokens > 0`. Runs on k8scratch (resized to the full definition-of-done target) and must pass three consecutive runs. This directory is reserved at R0 with this README only; **N1** authors the suite.
