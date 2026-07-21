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

Both live as separately-testable functions so the negative case -- an
unreviewed or check-less commit is refused -- is an ordinary pytest assertion
against a constructed fixture, not a manual demonstration against the real
repo. Only `main()` needs the network (`gh api` for the live check-run list);
see `release/integrity.py` for the same manifest/verify split rationale.
"""

from __future__ import annotations

import argparse
import json
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


def authorize(
    sha: str,
    check_runs: list[dict[str, object]],
    main_ref: str = "origin/main",
    *,
    cwd: Path | None = None,
) -> None:
    """Raise AuthorizationError unless `sha` may publish a release."""
    if not commit_is_on_reviewed_main(sha, main_ref, cwd=cwd):
        raise AuthorizationError(
            f"commit {sha} is not reachable from {main_ref}. A release can only "
            "publish from a commit that reached main through a reviewed, merged "
            "PR; refusing to authorize this tag."
        )
    if not required_checks_passed(check_runs):
        raise AuthorizationError(
            f"commit {sha} does not have a fully successful set of check-runs "
            f"({len(check_runs)} found). Refusing to authorize this tag until its "
            "required checks are current and green."
        )


def fetch_check_runs(sha: str, repo: str) -> list[dict[str, object]]:
    """The commit's check-runs via `gh api`, which carries GITHUB_TOKEN auth.

    `per_page=100` without pagination: this repo's own CI (see release.yaml's
    check list) runs well under 20 checks per commit, so a single page always
    covers it, and parsing one JSON object is simpler than merging paginated
    array-under-a-key responses (this endpoint's top level is an object, not
    an array, so `gh api --paginate` does not auto-merge it).
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs", "-f", "per_page=100"],
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
    args = parser.parse_args(argv)

    try:
        check_runs = fetch_check_runs(args.sha, args.repo)
        authorize(args.sha, check_runs, args.main_ref)
    except AuthorizationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.sha} is reviewed, on {args.main_ref}, and checked -- authorized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
