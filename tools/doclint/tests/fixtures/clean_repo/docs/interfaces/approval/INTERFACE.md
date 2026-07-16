---
seam: Approval
kind: SOFT
impls: 1 + fake
grade: B+
vision_row: Approval
epics:
  - "#430"
  - "ADR-0035"
order: 2
---

# Approval

<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 + fake &nbsp;·&nbsp; **Swap-readiness grade:** B+
<!-- END GENERATED: header -->

Current contract: the gate is `runner/src/agentos_runner/approval.py::authorize_approval`,
built on `runner/src/agentos_runner/approval.py::ApprovalGate` and its
`runner/src/agentos_runner/approval.py::ApprovalGate.consume_grant` method. The
option builder is re-exported here as
`runner/src/agentos_runner/approval.py::build_options`.
