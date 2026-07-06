# Documentation

This directory holds two kinds of document. Read the living docs for how the
system works today; the historical records are preserved as engineering history
and are not maintained.

## Living documentation

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md): the as-built architecture, with the
  component map and the message-flow and deploy-flow sequence diagrams. Start
  here.
- [`architecture.md`](architecture.md): a redirect stub pointing at the root
  `ARCHITECTURE.md` above.
- [`architecture-vision.md`](architecture-vision.md): the forward-looking
  "swappable jobs around an opinionated core" framing, covering where each
  adopted component's swap seam lives.
- [`roadmap.md`](roadmap.md): forward-looking work after the v0.1 MVP.
- [`adr/`](adr/): Architecture Decision Records, the load-bearing choices and
  the live evidence behind them.

## Historical records

Preserved as engineering history. They are not living documentation: they
describe the pre-build design and the de-risking that preceded the MVP, and are
not kept in sync with the code.

- [`mvp-build-plan.md`](mvp-build-plan.md): the architecture spine the MVP was
  built from.
- [`build-orchestration-plan.md`](build-orchestration-plan.md): the task DAG
  and how the build was orchestrated.
- [`prototype-derisking-review.md`](prototype-derisking-review.md): the
  pre-build review that settled the risky infrastructure bets.
- [`test-plans/`](test-plans/): the individual prototype de-risking test plans
  (PT-1 through PT-4) and their live results.
- [`reference/`](reference/): the pre-build reference and design documents.
