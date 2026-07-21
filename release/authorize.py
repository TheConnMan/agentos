#!/usr/bin/env python3
"""Decide whether a pushed tag is authorized to publish a release (issue #628).

`release.yaml` triggers its full publish pipeline on any `v*` tag push. A tag is
just a ref; pushing one proves nothing about the commit it points at. This
script is the gate `authorize-release` runs before any other job in that
pipeline, and it fails closed on either of two questions:

  ancestry  Is the tagged commit reachable from `origin/main`? A tag on a
            feature branch, a rebased-away commit, or anything that bypassed a
            merged PR is refused here, before any image builds.

  checks    Does that commit's check-runs list show every check successful (or
            neutral/skipped)? A commit that is on main but was never checked,
            or was checked and failed, is refused the same way. Zero check-runs
            is also a failure -- absence of checks is not evidence they passed.
            The gate's own workflow run is excluded from that list (issue
            #732): it is itself an in-progress check-run on the tagged SHA and
            would otherwise wait on itself forever.

Both live as separately-testable functions so the negative case -- an
unreviewed or check-less commit is refused -- is an ordinary pytest assertion
against a constructed fixture, not a manual demonstration against the real
repo. Only `main()` needs the network (`gh api` for the live check-run list);
see `release/integrity.py` for the same manifest/verify split rationale.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}


class AuthorizationError(Exception):
    """A tagged commit is not authorized to publish a release."""


def commit_is_on_reviewed_main(
    sha: str, main_ref: str = "origin/main", *, cwd: Path | None = None
) -> bool:
    """Is `sha` reachable from `main_ref` in the checked-out repo at `cwd`?

    Requires full history (`fetch-depth: 0` in the workflow checkout); a
    shallow clone would make an ancestor commit unreachable and read as absent.
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, main_ref],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def required_checks_passed(check_runs: list[dict[str, object]]) -> bool:
    """Did every check-run for the commit conclude successfully?

    An empty list fails: no checks having run is not the same as them passing,
    and a commit racing ahead of its own CI must not slip through on that gap.
    """
    if not check_runs:
        return False
    return all(run.get("conclusion") in PASSING_CONCLUSIONS for run in check_runs)


def exclude_current_workflow_run(
    check_runs: list[dict[str, object]], run_id: str | None
) -> list[dict[str, object]]:
    """Drop the check-runs belonging to the workflow run doing the asking.

    On a tag push the gate's own job is itself a check-run on the tagged SHA,
    with `status: in_progress` and `conclusion: null`, so it would refuse every
    legitimate release by waiting on itself. Only the current run is excluded --
    a blanket "ignore every null conclusion" would let a genuinely stuck
    unrelated required check through, which is the opposite of failing closed.

    A check-run's `details_url` is its job URL, observed live as
    `https://github.com/curie-eng/agentos/actions/runs/<run_id>/job/<job_id>`,
    so the run id embedded in that path is the ownership signal. An entry with
    no `details_url` (an external app's check) can never be ours, so it stays.
    """
    if not run_id:
        return check_runs
    marker = f"/actions/runs/{run_id}/"
    return [
        run for run in check_runs if marker not in str(run.get("details_url") or "")
    ]


def authorize(
    sha: str,
    check_runs: list[dict[str, object]],
    main_ref: str = "origin/main",
    *,
    cwd: Path | None = None,
    exclude_run_id: str | None = None,
) -> None:
    """Raise AuthorizationError unless `sha` may publish a release."""
    if not commit_is_on_reviewed_main(sha, main_ref, cwd=cwd):
        raise AuthorizationError(
            f"commit {sha} is not reachable from {main_ref}. A release can only "
            "publish from a commit that reached main through a reviewed, merged "
            "PR; refusing to authorize this tag."
        )
    other_runs = exclude_current_workflow_run(check_runs, exclude_run_id)
    if not required_checks_passed(other_runs):
        raise AuthorizationError(
            f"commit {sha} does not have a fully successful set of check-runs "
            f"({len(other_runs)} found, excluding this workflow run's own). "
            "Refusing to authorize this tag until its required checks are "
            "current and green."
        )


def fetch_check_runs(sha: str, repo: str) -> list[dict[str, object]]:
    """The commit's check-runs via `gh api`, which carries GITHUB_TOKEN auth.

    `per_page=100` without pagination: this repo's own CI (see release.yaml's
    check list) runs well under 20 checks per commit, so a single page always
    covers it, and parsing one JSON object is simpler than merging paginated
    array-under-a-key responses (this endpoint's top level is an object, not
    an array, so `gh api --paginate` does not auto-merge it).

    `-X GET` is load-bearing, not decoration: `gh api` defaults to GET but
    switches to POST as soon as any `-f`/`-F` flag is present, and
    `POST /repos/{owner}/{repo}/commits/{sha}/check-runs` does not exist
    (issue #732). The live observation pinning this is cited in
    `release/tests/test_authorize.py::TestFetchCheckRuns`.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "GET",
            f"repos/{repo}/commits/{sha}/check-runs",
            "-f",
            "per_page=100",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    runs: list[dict[str, object]] = payload["check_runs"]
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="the tagged commit to authorize")
    parser.add_argument("--repo", required=True, help="owner/name, e.g. curie-eng/agentos")
    parser.add_argument("--main-ref", default="origin/main", help="the reviewed-main ref")
    parser.add_argument(
        "--run-id",
        default=os.environ.get("GITHUB_RUN_ID"),
        help="this workflow run's id; its own check-runs are excluded from the gate",
    )
    args = parser.parse_args(argv)

    try:
        check_runs = fetch_check_runs(args.sha, args.repo)
        authorize(args.sha, check_runs, args.main_ref, exclude_run_id=args.run_id)
    except AuthorizationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        # A lookup that did not complete is not an authorization verdict. Fail
        # closed either way, but say which of the two happened -- an opaque
        # traceback here is what kept issue #732's `gh api` defect unreadable.
        detail = getattr(exc, "stderr", None)
        suffix = f": {str(detail).strip()}" if detail else ""
        print(
            f"ERROR: could not retrieve check-runs for {args.sha} from "
            f"{args.repo} -- the lookup failed with "
            f"{type(exc).__name__}{suffix}. Refusing to authorize this tag "
            "because its check status is unknown.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {args.sha} is reviewed, on {args.main_ref}, and checked -- authorized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
