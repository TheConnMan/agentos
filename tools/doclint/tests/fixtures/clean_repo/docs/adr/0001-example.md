# ADR-0001: Example (fixture)

Status: Accepted

ADRs are immutable once Accepted, so their historical citations are NOT linted.
This line deliberately carries a rotten coordinate `queue.py:60` and a
nonexistent path `apps/ghost/does_not_exist.py`, and the clean tree must still
pass because `docs/adr/` is excluded from the linted root.
