"""Git-flow engine (J1): turn a pushed commit into a deploy or a promote.

A push to the dev branch builds the plugin bundle at that commit and deploys it
under the dev bot identity; a push to the prod branch promotes the same commit
(reusing the already-built bundle when present) under the prod bot identity. The
bundle is produced by archiving the pushed sha from the repo, so the flow needs
only git-protocol access to the remote (local bare repos in tests) and never the
GitHub API.
"""

import hashlib
import hmac
import os
import shutil
import subprocess
import tempfile

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from . import bundles, crud, deploy
from .config import Settings
from .models import Environment
from .schemas import WebhookResult
from .storage import BundleStore

_ZERO_SHA = "0" * 40


class GitFlowError(Exception):
    """The repo could not be fetched or archived at the requested commit."""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's X-Hub-Signature-256 over the raw body."""

    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def environment_for_ref(ref: str | None, settings: Settings) -> Environment | None:
    """Map a git ref to an environment, or None if it is not a deploy branch.

    The full ref is matched (refs/heads/<branch>), never just the last path
    segment, so a tag named `main` or a branch `feature/dev` cannot masquerade
    as a deploy branch.
    """

    if not ref:
        return None
    if ref == f"refs/heads/{settings.dev_branch}":
        return Environment.dev
    if ref == f"refs/heads/{settings.prod_branch}":
        return Environment.prod
    return None


def clone_and_archive(clone_url: str, sha: str, settings: Settings) -> bytes:
    """Mirror-clone the repo and return a tar of the tree at ``sha``.

    Refuses clone URLs outside the configured scheme allowlist and restricts git
    to safe transports, so a webhook cannot coerce an arbitrary git command.
    """

    if not clone_url.startswith(settings.git_allowed_schemes):
        raise GitFlowError(f"clone url scheme not allowed: {clone_url}")

    tmp = tempfile.mkdtemp(prefix="gitflow-")
    env = {
        **os.environ,
        "GIT_ALLOW_PROTOCOL": "file:https:http",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--mirror", clone_url, tmp],
                check=True,
                capture_output=True,
                env=env,
                timeout=120,
            )
            archived = subprocess.run(
                ["git", "-C", tmp, "archive", "--format=tar", sha],
                check=True,
                capture_output=True,
                env=env,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitFlowError(f"could not archive {sha[:12]}: {exc}") from exc
        return archived.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def process_push(
    session: AsyncSession,
    store: BundleStore,
    settings: Settings,
    payload: dict[str, object],
) -> WebhookResult:
    """Deploy (dev) or promote (prod) the pushed commit; ignore other refs."""

    ref = payload.get("ref")
    after = payload.get("after")
    repo = payload.get("repository")
    environment = environment_for_ref(ref if isinstance(ref, str) else None, settings)
    if environment is None:
        return WebhookResult(status="ignored")
    if not isinstance(after, str) or after == _ZERO_SHA:
        return WebhookResult(status="ignored")
    if not isinstance(repo, dict):
        return WebhookResult(status="ignored")

    full_name = repo.get("full_name")
    clone_url = repo.get("clone_url") or repo.get("url")
    if not isinstance(full_name, str) or not isinstance(clone_url, str):
        return WebhookResult(status="ignored")

    agent = await crud.get_agent_by_repo(session, full_name)
    if agent is None:
        return WebhookResult(status="ignored")

    version = await crud.get_version_by_commit(session, agent.id, after)
    # Only a version whose bundle is actually stored may be reused for promote.
    # A row with bundle_ref still None is the residue of a prior attempt that
    # failed after the row committed; rebuild and store into it rather than
    # deploying a bundleless version.
    if version is None or version.bundle_ref is None:
        try:
            data = await run_in_threadpool(
                clone_and_archive, clone_url, after, settings
            )
            extension, content_type = deploy.validate_archive(data)
        except GitFlowError as exc:
            return WebhookResult(
                status="rejected",
                errors=[{"code": "git.archive_failed", "message": str(exc)}],
            )
        except bundles.UnsupportedArchive as exc:
            return WebhookResult(
                status="rejected",
                errors=[{"code": "bundle.unsupported", "message": str(exc)}],
            )
        except deploy.BundleInvalid as exc:
            return WebhookResult(status="rejected", errors=exc.errors)

        if version is None:
            version = await crud.create_version_row(
                session,
                agent.id,
                version_label=after[:12],
                created_by="git-flow",
                commit_sha=after,
            )
        await deploy.store_bundle(
            store, session, agent.id, version, data, extension, content_type
        )

    bot_identity = (
        settings.bot_identity_prod
        if environment is Environment.prod
        else settings.bot_identity_dev
    )
    deployment = await crud.create_deployment_row(
        session,
        agent.id,
        version.id,
        environment,
        bot_identity=bot_identity,
        commit_sha=after,
    )
    return WebhookResult(
        status="promoted" if environment is Environment.prod else "deployed",
        environment=environment,
        bot_identity=bot_identity,
        agent_id=agent.id,
        version_id=version.id,
        deployment_id=deployment.id,
        commit_sha=after,
    )
