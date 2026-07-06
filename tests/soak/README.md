# tests/soak

This directory is reserved for the soak and chaos suite. The suite is not yet
authored.

When written, it proves the definition-of-done under sustained load: concurrent
threads plus a mid-thread batch job, a sandbox killed mid-run, and a
resume-rehydrate, asserting no cross-talk between threads, no duplicate side
effects, and sandbox-affined `cache_read_input_tokens > 0`. It runs against a
real cluster sized to the full definition-of-done target and must pass three
consecutive runs.
