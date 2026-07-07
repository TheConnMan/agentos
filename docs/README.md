# Documentation

Read the living docs below for how the system works today. The pre-build design
and de-risking documents were removed after the MVP shipped and are preserved in
git history (`git log -- docs/`).

## Living documentation

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md): the as-built architecture, with the
  component map and the message-flow and deploy-flow sequence diagrams. Start
  here.
- [`architecture.md`](architecture.md): a redirect stub pointing at the root
  `ARCHITECTURE.md` above.
- [`architecture-vision.md`](architecture-vision.md): the forward-looking
  "swappable jobs around an opinionated core" framing, covering where each
  adopted component's swap seam lives.
- [`operations.md`](operations.md): running a cluster install, plus
  operator-facing findings from early installs.

Forward-looking work is planned and tracked in
[GitHub issues](https://github.com/curie-eng/agentos/issues), with larger
journeys filed as `epic`-labeled issues.
- [`adr/`](adr/): Architecture Decision Records, the load-bearing choices and
  the live evidence behind them.
