# 64. Fail-forward cluster teardown

Date: 2026-07-21

Status: Accepted

## Context

`curie cluster down` runs a two-step teardown: `helm uninstall` followed by a
label-scoped namespace sweep that deletes only the namespaces THIS release
created (`curietech.ai/created-by=<release>`, the ownership label introduced by
issue #707). The sweep is Helm-independent by design: it is what actually stops
compute (runtime sandboxes, PVCs, job pods Helm does not own).

The old flow bailed the instant `helm uninstall` returned a non-"not found"
nonzero exit. On a transient API-server blip the process exited non-zero and the
sweep never ran, orphaning both run-created namespaces (15+ live pods) with no
resume path surfaced (issue #767). This is fail-hard: the one step that stops
compute is skipped because a preceding, less important step failed.

## Decision

Make teardown fail-forward. Run both teardown steps to completion regardless of
the first step's outcome, then decide the exit from the combined result:

- `helm uninstall` is classified into removed / already-absent / failed. A
  failed helm uninstall no longer bails; its stderr is kept and control falls
  through to the sweep.
- The namespace sweep runs unconditionally and stays ownership-label-scoped
  (issue #707 is preserved exactly; the selector is never widened to an
  unconditional namespace delete).
- Two pure functions decide the outcome with no I/O: `outstanding_steps` maps the
  two step outcomes to the subset that did not complete, and `resume_command`
  renders those outstanding steps back into a copy-pasteable line by reusing the
  matching `down_commands` entries (the single source of truth for the teardown
  argv, so the resume line cannot drift from what would finish the job).
- An incomplete teardown returns a transient error (exit 3) carrying the exact
  resume command in the ADR-0021 `{error, fix}` payload, with a message that
  distinguishes "namespaces swept, only the helm release record remains" from
  "nothing could be removed; the API server is unreachable".

We chose the transient `Err` path over a new `ClusterDownOutput` success
variant: an incomplete teardown is an error, not a success. Exit 3 is precisely
"retry once the condition clears", and the resume command already has a canonical
home in `fix`. A partial-success success variant would re-introduce the ticket's
own failure mode (teardown reports success while compute keeps running) and force
a new JSON shape to carry the resume command, churning the pinned success-path
contract.

## Consequences

- Compute stops even when helm discovery fails: the sweep is attempted on every
  `down`, so a transient blip no longer strands run-created namespaces.
- Operators and CI get a retryable exit 3 plus a copy-pasteable resume command
  instead of an opaque bail; the resume line is a subset of the same commands the
  run just attempted.
- No success-path JSON contract churn: `Down` / `Aborted` / `DryRun` and their
  pinned shapes are untouched. Only the error path is new, and it rides the
  existing centralized error emit.
- The ownership-scope invariant (issue #707) is upheld: fail-forward deletion
  stays label-scoped; a pre-existing or unlabeled namespace is never touched.
