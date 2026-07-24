# Eval fan-out seam: `curie:evals`

The API is the producer of eval jobs; a worker consumer (a separate worker-lane
task) runs them. This documents the wire contract so the consumer can be built
against it, the same way the dispatcher documents `curie:runs`.

## When the API enqueues

On a **dev-branch push** the git-flow deploy (`gitflow.process_push`, environment
`dev`) enqueues exactly one eval job for the new version, after the bundle is
stored and the deployment row is written. A prod promote does **not** fan out
evals. Other refs, unknown repos, and rejected bundles never enqueue.

## Stream and wire encoding

- Stream: `curie:evals` (distinct from the dispatcher's `curie:runs`).
- Encoding mirrors `curie:runs` exactly: one stream field named `payload`
  holds the model as `model_dump_json()`. A one-field JSON blob keeps the seam
  explicit and versionable (add fields without reshaping the stream schema).
- The producer and consumer share `aci_protocol.EvalJob` (`packages/aci-protocol`);
  the consumer reconstructs it with `aci_protocol.parse_eval_job(entry_fields[...])`.

## Payload fields (`EvalJob`)

| field | type | meaning |
|---|---|---|
| `agent_id` | uuid | the agent whose version was pushed |
| `version_id` | uuid | the `agent_versions` row for this commit |
| `sha` | str | the pushed commit sha (also the version tag) |
| `suite` | str | the eval suite name to run (default `default`) |
| `bundle_ref` | str \| null | the stored bundle key for the version |
| `target_url` | str \| null | optional runner base_url override |
| `requested_at` | str | ISO-8601 UTC enqueue time |

## Consumer responsibilities (not built here)

Read the stream with a consumer group, reconstruct the `EvalJob`, run
`python -m curie_worker.eval` for the version (resolving the suite JSON from the
bundle and pointing it at a runner), and — on completion — POST the rollup to the
API's `POST /evals/report` so the GitHub commit status is set. The recorder
already writes `eval_pass` scores that the API's `GET /evals/matrix` reads back.
