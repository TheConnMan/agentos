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

<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 + fake &nbsp;·&nbsp; **Swap-readiness grade:** B+
<!-- END GENERATED: header -->

Current contract: the gate is `runner/src/curie_runner/approval.py::authorize_approval`,
built on `runner/src/curie_runner/approval.py::ApprovalGate` and its
`runner/src/curie_runner/approval.py::ApprovalGate.consume_grant` method. The
option builder is re-exported here as
`runner/src/curie_runner/approval.py::build_options`.
