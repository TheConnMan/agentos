"""F3 eval-stream consumer: run eval suites requested on the agentos:evals stream.

The K1-API producer XADDs one stream field ``payload`` holding an ``EvalJob``
JSON (the dispatcher seam convention). This consumer runs a distinct consumer
group (``agentos-eval-workers``) so it does not compete with the runs consumer.
For each entry it:

  1. fetches the version's immutable bundle from MinIO by ``bundle_ref`` and loads
     the suite from the bundle's own ``evals/cases.json`` (the same shape the CLI's
     ``agentos skill eval`` reads); the ``suite`` field names it and tags Langfuse;
  2. runs the suite against the runner: ``target_url`` if given (the dev/test
     shortcut), otherwise provisions a sandbox for the version via the G1
     substrate (the same boot env F2 uses) and tears it down in a finally;
  3. records per-case scores to Langfuse (keyed by version, the shape the matrix
     endpoint reads) and POSTs a summary to the platform API's ``/evals/report``.

Delivery semantics: an entry is XACKed only after the report POST attempt
completes (success, or terminally failed after bounded retries, logged). A worker
crash before that attempt leaves the entry pending, so the next redelivery re-runs
it -- an eval is never lost to a mid-run crash. A malformed payload cannot be
processed on any redelivery, so it is logged and acked (a poison-pill drop). A
missing/corrupt bundle or a provisioning failure is a failed run reported and
acked, not a crash; a failing eval case is a failed count in the report.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
from aci_protocol import (
    STREAM_PAYLOAD_FIELD,
    Budget,
    EvalJob,
    EvalReport,
    parse_eval_job,
)
from plugin_format import safe_extract
from redis.asyncio import Redis

from ..binding import (
    BUDGET_ENV,
    BUNDLE_REF_ENV,
    PLUGIN_DIR_ENV,
    RUNNER_TOKEN_ENV,
    SESSION_ID_ENV,
    apply_model_env,
    inject_connector_secrets,
)
from ..bundle_store import BundleReader
from ..config import WorkerConfig
from ..sandbox import SandboxSubstrate
from ..sandbox.types import SandboxError
from ..stream_consumer import ReadLoopSpec, StreamConsumer, StreamEntry
from .models import EvalRunResult, EvalSuite
from .recorder import LangfuseEvalRecorder
from .run import run_eval_suite

logger = logging.getLogger(__name__)

# ``EvalJob``/``EvalReport`` are the shared wire models (#492), re-exported so
# this module stays the eval lane's seam for the stream payloads.
__all__ = [
    "EvalJob",
    "EvalReport",
    "EvalReporter",
    "EvalStreamConsumer",
    "load_suite_from_bundle",
]

# Pause before retrying the blocking eval read after a transient transport error.
_EVAL_READ_ERROR_BACKOFF_S = 0.5


class EvalReporter:
    """POSTs an EvalReport to the platform API's /evals/report, with retries."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        max_attempts: int = 3,
        backoff_base_s: float = 0.5,
    ) -> None:
        self._url = f"{api_base_url.rstrip('/')}/evals/report"
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s

    async def report(self, report: EvalReport) -> bool:
        """Attempt the report POST with bounded retries. True on success; False
        when terminally failed (logged) -- either way the caller may ack."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._client.post(
                    self._url, json=report.model_dump(), headers=self._headers
                )
                resp.raise_for_status()
                return True
            except httpx.HTTPError as exc:
                if attempt >= self._max_attempts:
                    logger.error(
                        "eval report POST failed terminally for %s@%s: %s",
                        report.repo_full_name,
                        report.sha,
                        exc,
                    )
                    return False
                await asyncio.sleep(self._backoff_base_s * (2 ** (attempt - 1)))
        return False


def load_suite_from_bundle(data: bytes, suite_name: str) -> EvalSuite | None:
    """Extract the bundle archive and load its evals/cases.json as an EvalSuite,
    named with ``suite_name`` (the payload's authoritative name / Langfuse tag).
    Returns None on a corrupt archive or a missing/invalid suite file."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            safe_extract(data, dest)
            cases = next(
                (p for p in dest.rglob("cases.json") if p.parent.name == "evals"), None
            )
            if cases is None:
                return None
            loaded = EvalSuite.model_validate_json(cases.read_text())
    except Exception:
        logger.exception("could not load eval suite from bundle")
        return None
    return EvalSuite(name=suite_name, cases=loaded.cases)


class EvalStreamConsumer(StreamConsumer):
    """Reads agentos:evals, runs each suite, records, reports, then acks."""

    def __init__(
        self,
        *,
        redis: Redis,
        config: WorkerConfig,
        bundle_store: BundleReader,
        substrate: SandboxSubstrate,
        reporter: EvalReporter,
        recorder: LangfuseEvalRecorder,
        repo_lookup: Any,
    ) -> None:
        super().__init__(redis)
        self._config = config
        self._bundles = bundle_store
        self._substrate = substrate
        self._reporter = reporter
        self._recorder = recorder
        self._repo_lookup = repo_lookup
        # Entry ids this consumer is handling right now. XAUTOCLAIM would
        # otherwise reclaim our own still-pending (long-running) eval and
        # re-run it in parallel; skipping these ids prevents that self-reclaim.
        self._inflight_ids: set[str] = set()

    async def ensure_group(self) -> None:
        # Create the group at a max-age cutoff rather than the stream head ("0").
        # Reading from "0" replays the ENTIRE stream history the first time the
        # group is created, so a stream that accumulated ancient entries (across
        # deploys, weeks of PRs) storms them all into the worker at once on first
        # boot. Starting at (now - eval_stream_max_age_hours) skips only long-dead
        # entries while still delivering recent backlog: an eval younger than the
        # window is never lost -- a short outage is covered -- but a week-old
        # requeue is not replayed. Crash recovery is unaffected: the reclaim loop
        # works off the pending list, not the group's start id. An existing group
        # keeps its position, so this only bounds the very first creation.
        cutoff_ms = int(
            (
                datetime.now(UTC)
                - timedelta(hours=self._config.eval_stream_max_age_hours)
            ).timestamp()
            * 1000
        )
        await self._ensure_group(
            self._config.eval_stream,
            self._config.eval_consumer_group,
            start_id=str(cutoff_ms),
        )

    async def run(self) -> None:
        await self.ensure_group()
        await asyncio.gather(self._read_loop(), self._reclaim_loop())

    async def _read_loop(self) -> None:
        # count=1 is load-bearing, not a tunable: this consumer handles an entry
        # inline (evals are heavy and run sequentially), and only the entry inside
        # _handle is tracked as in-flight. Claiming a batch would leave the
        # un-handled tail claimed-but-untracked, where the reclaim loop could
        # re-run one before the read loop reaches it (a duplicate report). Peer
        # replicas in the group provide the parallelism instead.
        await self._consume(
            ReadLoopSpec(
                stream=self._config.eval_stream,
                group=self._config.eval_consumer_group,
                consumer=self._config.eval_consumer_name,
                count=1,
                block_ms=self._config.read_block_ms,
                backoff_s=_EVAL_READ_ERROR_BACKOFF_S,
                timeout_msg="eval stream read timed out (idle); retrying: %s",
                connection_msg="eval stream read failed transiently; retrying: %s",
                logger=logger,
            ),
            self._handle,
        )

    async def _reclaim_loop(self) -> None:
        """Periodically reclaim entries a dead consumer left pending and re-run
        them. Without this, an entry left pending by a crash before ``_ack``
        would sit in the group's PEL forever -- the at-least-once promise the
        ``_handle`` pending path relies on lives here."""
        while not self._stop.is_set():
            try:
                await self._reclaim_once()
            except Exception:
                logger.exception("eval reclaim tick failed")
            await self._sleep_or_stop(self._config.reclaim_interval_s)

    async def _reclaim_once(self) -> int:
        reclaimed = 0
        cursor: str = "0-0"
        while not self._stop.is_set():
            raw = await self._redis.xautoclaim(
                self._config.eval_stream,
                self._config.eval_consumer_group,
                self._config.eval_consumer_name,
                min_idle_time=self._config.reclaim_min_idle_ms,
                start_id=cursor,
                count=self._config.read_count,
            )
            cursor = str(raw[0])
            entries = cast("list[StreamEntry]", raw[1])
            for entry_id, fields in entries:
                if entry_id in self._inflight_ids:
                    continue  # still being handled here; not an orphan
                reclaimed += 1
                await self._handle(entry_id, fields)
            if cursor in ("0-0", "0"):
                break
        return reclaimed

    async def _handle(self, entry_id: str, fields: dict[str, str]) -> None:
        self._inflight_ids.add(entry_id)
        try:
            try:
                # The sanctioned tolerant decode: a newer API adding an optional
                # field must not land in the poison-pill branch below, which
                # would ack and DROP the job with no dead letter.
                item = parse_eval_job(fields[STREAM_PAYLOAD_FIELD])
            except Exception:
                # Poison pill: unprocessable on any redelivery, so ack and drop.
                logger.exception("malformed eval work item %s; acking as poison", entry_id)
                await self._ack(entry_id)
                return
            try:
                result = await self._run_and_report(item)
                logger.info("eval %s @ %s: %s", item.suite, item.sha, result.summary())
            except Exception:
                # An unexpected error before the report attempt: leave pending so
                # the reclaim loop re-runs it (an eval must not be lost to a crash).
                logger.exception("eval processing failed for %s; left pending", entry_id)
                return
            await self._ack(entry_id)
        finally:
            self._inflight_ids.discard(entry_id)

    async def _run_and_report(self, item: EvalJob) -> EvalRunResult:
        repo = await self._repo_lookup.repo_full_name(item.agent_id)
        suite = await self._load_suite(item)
        if suite is None:
            return await self._report_failed(item, repo, "unresolvable suite/bundle")

        base_url, release_key, token = await self._acquire_target(item)
        if base_url is None:
            return await self._report_failed(item, repo, "runner provisioning failed")
        try:
            result = await run_eval_suite(
                suite,
                base_url=base_url,
                version=item.sha,
                recorder=self._recorder,
                token=token,
                model=self._eval_model(item),
            )
        finally:
            if release_key is not None:
                await asyncio.to_thread(self._substrate.release, release_key)

        await self._report(item, repo, result)
        return result

    async def _load_suite(self, item: EvalJob) -> EvalSuite | None:
        if item.bundle_ref is None:
            return None
        try:
            data = await asyncio.to_thread(self._bundles.get, item.bundle_ref)
        except Exception:
            logger.exception("could not fetch bundle %s", item.bundle_ref)
            return None
        return load_suite_from_bundle(data, item.suite)

    async def _acquire_target(
        self, item: EvalJob
    ) -> tuple[str | None, str | None, str | None]:
        if item.target_url is not None:
            # dev/test shortcut: eval a given runner. Not a claim of ours, so no
            # token -- the driver omits the header (only-when-configured).
            return item.target_url, None, None
        release_key = f"eval-{uuid.uuid4().hex}"
        try:
            connector_secrets = await self._repo_lookup.secrets_for(item.agent_id)
            env = self._boot_env(item, connector_secrets)
            handle = await asyncio.to_thread(
                self._substrate.claim, release_key, env=env
            )
        except SandboxError:
            logger.exception("could not provision a runner for eval %s", item.sha)
            return None, None, None
        return handle.base_url, release_key, handle.token or None

    def _eval_model(self, item: EvalJob) -> str | None:
        """The model dimension for this run: the caller-requested ``item.model``
        when set (#526, a sweep pins each run to a distinct model), else the model
        the eval's runner is booted with (``config.model``, the same value
        ``apply_model_env`` forwards as ``AGENTOS_MODEL``). A requested model is
        always the label, so a sweep run is never silently unlabelled. The dev/test
        ``target_url`` shortcut evals a runner we did not boot, so unless the caller
        named a model its model is unknown and left unlabelled."""
        if item.model is not None:
            return item.model
        if item.target_url is not None:
            return None
        return self._config.model or None

    def _boot_env(
        self, item: EvalJob, connector_secrets: dict[str, str] | None = None
    ) -> dict[str, str]:
        budget = Budget(
            max_output_tokens_per_run=self._config.default_max_output_tokens_per_run,
            max_usd_per_day=self._config.default_max_usd_per_day,
        )
        env = {
            BUDGET_ENV: budget.model_dump_json(),
            SESSION_ID_ENV: f"eval-{item.version_id}",
            PLUGIN_DIR_ENV: self._config.bundle_plugin_dir,
            RUNNER_TOKEN_ENV: secrets.token_urlsafe(32),
        }
        if item.bundle_ref is not None:
            env[BUNDLE_REF_ENV] = item.bundle_ref
        # Deliver the agent's connector secrets (#429) so an authed-MCP bundle
        # authenticates during eval exactly as it does on a bound run -- otherwise
        # its tool calls fail auth and the eval measures the wrong thing. The
        # shared helper drops reserved boot-env names (#457) so a secret named after
        # a runner-owned env key or model credential can never clobber it, keeping
        # this write site hardened identically to binding.boot_env. The values are
        # resolved by the async caller (they need a DB lookup) and passed in.
        inject_connector_secrets(env, connector_secrets, agent_label=item.agent_id)
        # A caller-requested model (#526) wins over the worker default so the
        # provisioned sandbox actually runs the model this sweep row is measuring;
        # _eval_model tags the same value, keeping the boot and the matrix label
        # in lock-step. None falls back to config.model exactly as before.
        apply_model_env(env, self._config, model_override=item.model)
        return env

    async def _report_failed(
        self, item: EvalJob, repo: str | None, reason: str
    ) -> EvalRunResult:
        logger.error("eval %s @ %s failed: %s", item.suite, item.sha, reason)
        result = EvalRunResult(version=item.sha, suite=item.suite, results=[])
        await self._report(item, repo, result)
        return result

    async def _report(
        self, item: EvalJob, repo: str | None, result: EvalRunResult
    ) -> None:
        await self._reporter.report(
            EvalReport(
                repo_full_name=repo or str(item.agent_id),
                sha=item.sha,
                passed_count=result.passed_count,
                total=result.total,
                target_url=item.target_url,
            )
        )

    async def _ack(self, entry_id: str) -> None:
        await self._xack(
            self._config.eval_stream, self._config.eval_consumer_group, entry_id
        )
