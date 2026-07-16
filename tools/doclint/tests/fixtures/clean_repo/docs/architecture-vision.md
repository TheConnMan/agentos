# Architecture vision (fixture)

The swap-readiness table below is the authority each graded seam doc names via
its front-matter `vision_row:` key. Grade cells carry a bare grade token here;
the real table appends a rationale after a colon, which the check ignores.

## Swap readiness

| Job | Port contract | Current adapter | Grade | Cheapest next step |
|---|---|---|---|---|
| Substrate | SandboxClient port | Kubernetes and Docker | A | None needed |
| Approval | ApprovalGate | one gate plus a recording fake | B+ | Add a second approver |
