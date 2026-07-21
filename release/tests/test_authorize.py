"""Contract tests for the release authorization gate (release/authorize.py).

The gate is the deterministic check behind issue #628: a `v*` tag must not be
able to start the publish pipeline unless its commit is reachable from
`origin/main` and that commit's checks are all green. These tests drive both
functions directly -- `commit_is_on_reviewed_main` against a real, disposable
git repo (no network needed for ancestry) and `required_checks_passed` against
constructed check-run lists -- plus `authorize()`, which combines them and is
what `authorize-release` actually calls.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "release" / "authorize.py"


def load_module():
    """Import the standalone script by path (release/ is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("release_authorize", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


authorize_module = load_module()


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-m", f"add {name}")
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A repo with a `main` branch, plus commits diverging on an unmerged branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    commit(repo, "on-main.txt")
    return repo


CHECK_RUNS_ALL_GREEN = [
    {"name": "CI", "conclusion": "success"},
    {"name": "CodeQL", "conclusion": "neutral"},
    {"name": "Secret Scan", "conclusion": "skipped"},
]
CHECK_RUNS_ONE_FAILED = [
    {"name": "CI", "conclusion": "success"},
    {"name": "Dependency Audit", "conclusion": "failure"},
]

CURRENT_RUN_ID = "29811627398"
OTHER_RUN_ID = "29811600001"


def check_run(name: str, conclusion: str | None, run_id: str, job_id: str) -> dict:
    """A check-run shaped like the live API response (issue #732).

    `details_url` observed live on this repo:
    https://github.com/curie-eng/agentos/actions/runs/29811627398/job/88573652086
    """
    return {
        "name": name,
        "status": "completed" if conclusion is not None else "in_progress",
        "conclusion": conclusion,
        "details_url": (
            f"https://github.com/curie-eng/agentos/actions/runs/{run_id}/job/{job_id}"
        ),
    }


GATE_OWN_IN_PROGRESS = check_run(
    "authorize-release", None, CURRENT_RUN_ID, "88573652086"
)


class TestCommitIsOnReviewedMain:
    def test_head_of_main_is_reachable(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        assert authorize_module.commit_is_on_reviewed_main(sha, "main", cwd=git_repo)

    def test_ancestor_of_main_is_reachable(self, git_repo):
        first = run_git(git_repo, "rev-parse", "HEAD")
        commit(git_repo, "later-on-main.txt")

        assert authorize_module.commit_is_on_reviewed_main(first, "main", cwd=git_repo)

    def test_commit_only_on_an_unmerged_branch_is_refused(self, git_repo):
        run_git(git_repo, "checkout", "-q", "-b", "feature")
        unmerged = commit(git_repo, "feature-only.txt")

        assert not authorize_module.commit_is_on_reviewed_main(
            unmerged, "main", cwd=git_repo
        )

    def test_unknown_sha_is_refused_not_raised(self, git_repo):
        assert not authorize_module.commit_is_on_reviewed_main(
            "0" * 40, "main", cwd=git_repo
        )


class TestRequiredChecksPassed:
    def test_all_success_or_neutral_or_skipped_passes(self):
        assert authorize_module.required_checks_passed(CHECK_RUNS_ALL_GREEN)

    def test_any_failure_fails(self):
        assert not authorize_module.required_checks_passed(CHECK_RUNS_ONE_FAILED)

    def test_no_check_runs_fails(self):
        # Absence of checks is not evidence they passed.
        assert not authorize_module.required_checks_passed([])


class TestAuthorize:
    def test_reviewed_and_green_commit_is_authorized(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        authorize_module.authorize(sha, CHECK_RUNS_ALL_GREEN, "main", cwd=git_repo)

    def test_unreviewed_commit_is_refused_even_with_green_checks(self, git_repo):
        run_git(git_repo, "checkout", "-q", "-b", "feature")
        unmerged = commit(git_repo, "feature-only.txt")

        with pytest.raises(authorize_module.AuthorizationError, match="not reachable"):
            authorize_module.authorize(
                unmerged, CHECK_RUNS_ALL_GREEN, "main", cwd=git_repo
            )

    def test_reviewed_commit_with_failed_checks_is_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        with pytest.raises(
            authorize_module.AuthorizationError, match="check-runs"
        ):
            authorize_module.authorize(sha, CHECK_RUNS_ONE_FAILED, "main", cwd=git_repo)


class TestFetchCheckRuns:
    """`gh api` must be pinned to GET (issue #732, defect 1).

    `gh api` defaults to GET but switches to POST as soon as any `-f`/`-F`
    flag is present, and `POST /repos/{owner}/{repo}/commits/{sha}/check-runs`
    does not exist. Verified live against curie-eng/agentos on 2026-07-21 at
    commit 276774ff: the `-f`-only form returned
    `{"message": "Not Found", "status": "404"}`, while adding `-X GET`
    returned `{"total_count": 42, ...}`. Mocking `gh` here is correct: GitHub
    is an external service.
    """

    @staticmethod
    def _capture(monkeypatch, payload: dict) -> list:
        captured: list = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(authorize_module.subprocess, "run", fake_run)
        return captured

    def test_check_runs_are_fetched_with_an_explicit_get(self, monkeypatch):
        payload = {"total_count": 1, "check_runs": [CHECK_RUNS_ALL_GREEN[0]]}
        captured = self._capture(monkeypatch, payload)

        runs = authorize_module.fetch_check_runs("deadbeef", "curie-eng/agentos")

        argv = captured[0]
        endpoint = "repos/curie-eng/agentos/commits/deadbeef/check-runs"
        assert "-X" in argv
        assert argv[argv.index("-X") + 1] == "GET"
        assert argv.index("-X") < argv.index(endpoint)
        assert "-f" in argv
        assert argv[argv.index("-f") + 1] == "per_page=100"
        assert runs == [CHECK_RUNS_ALL_GREEN[0]]


class TestExcludeCurrentWorkflowRun:
    """The gate is itself a check-run on the tagged SHA (issue #732, defect 2)."""

    def test_current_run_entries_are_dropped_and_others_survive(self):
        other = check_run("CI", "success", OTHER_RUN_ID, "88573600001")

        remaining = authorize_module.exclude_current_workflow_run(
            [GATE_OWN_IN_PROGRESS, other], CURRENT_RUN_ID
        )

        assert remaining == [other]

    def test_falsy_run_id_leaves_the_list_untouched(self):
        runs = [GATE_OWN_IN_PROGRESS, check_run("CI", "success", OTHER_RUN_ID, "1")]

        assert authorize_module.exclude_current_workflow_run(runs, None) == runs
        assert authorize_module.exclude_current_workflow_run(runs, "") == runs

    def test_entries_without_details_url_are_never_dropped(self):
        external = {"name": "External Check", "status": "completed", "conclusion": "success"}

        remaining = authorize_module.exclude_current_workflow_run(
            [GATE_OWN_IN_PROGRESS, external], CURRENT_RUN_ID
        )

        assert remaining == [external]


class TestAuthorizeExcludesCurrentRun:
    def test_gate_own_in_progress_entry_does_not_block_authorization(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "neutral", OTHER_RUN_ID, "88573600002"),
        ]

        authorize_module.authorize(
            sha, runs, "main", cwd=git_repo, exclude_run_id=CURRENT_RUN_ID
        )

    def test_unrelated_in_progress_check_is_still_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("Integration Tests", None, OTHER_RUN_ID, "88573600003"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="check-runs"):
            authorize_module.authorize(
                sha, runs, "main", cwd=git_repo, exclude_run_id=CURRENT_RUN_ID
            )

    def test_failed_check_from_another_run_is_still_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("Dependency Audit", "failure", OTHER_RUN_ID, "88573600004"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="check-runs"):
            authorize_module.authorize(
                sha, runs, "main", cwd=git_repo, exclude_run_id=CURRENT_RUN_ID
            )

    def test_only_current_run_checks_is_refused(self, git_repo):
        # Nothing survives filtering, and absence of checks is not evidence
        # they passed.
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("authorize-release setup", "success", CURRENT_RUN_ID, "88573652087"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="check-runs"):
            authorize_module.authorize(
                sha, runs, "main", cwd=git_repo, exclude_run_id=CURRENT_RUN_ID
            )


class TestMain:
    """`main()` must thread `GITHUB_RUN_ID` into `authorize()` as
    `exclude_run_id` (issue #732, defect 2). Every test above drives
    `authorize()` or `exclude_current_workflow_run()` directly, so deleting
    `exclude_run_id=args.run_id` from `main()`'s `authorize(...)` call leaves
    the whole suite green while restoring the bug: the gate would wait
    forever on its own in-progress check-run. These tests invoke `main()`
    itself so that wiring is covered at the layer where it actually lived.

    `fetch_check_runs` is stubbed so no network call happens; `authorize()`
    runs for real against `git_repo`, so `main()` is run with that repo as
    the working directory (`main()` calls `authorize()` without a `cwd`).
    """

    @staticmethod
    def _stub_fetch_check_runs(monkeypatch, runs: list[dict]) -> None:
        monkeypatch.setattr(
            authorize_module, "fetch_check_runs", lambda sha, repo: runs
        )

    @staticmethod
    def _runs_with_gate_own_in_progress() -> list[dict]:
        return [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "neutral", OTHER_RUN_ID, "88573600002"),
        ]

    def test_main_authorizes_when_github_run_id_excludes_its_own_check(
        self, git_repo, monkeypatch
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", CURRENT_RUN_ID)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/agentos", "--main-ref", "main"]
        )

        assert exit_code == 0

    def test_main_refuses_when_github_run_id_is_absent(self, git_repo, monkeypatch):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/agentos", "--main-ref", "main"]
        )

        assert exit_code == 1

    def test_main_refuses_when_github_run_id_is_a_different_run(
        self, git_repo, monkeypatch
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", OTHER_RUN_ID)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/agentos", "--main-ref", "main"]
        )

        assert exit_code == 1


class TestMainLookupFailures:
    """A failed check-run lookup must refuse legibly, not traceback (#732).

    Before this, `main()` caught only `AuthorizationError`, so every failure
    inside `fetch_check_runs` escaped as an unhandled traceback. The exit code
    was already 1, so the gate did fail closed; what was missing was any way
    for an operator to tell an unauthorized tag from a lookup that never
    completed -- which is exactly why the `gh api` POST/GET defect read as an
    opaque crash. Each path below must still return 1.
    """

    @staticmethod
    def _stub_fetch_raising(monkeypatch, exc: BaseException) -> None:
        def raising(sha, repo):
            raise exc

        monkeypatch.setattr(authorize_module, "fetch_check_runs", raising)

    @staticmethod
    def _run_main(git_repo, monkeypatch) -> int:
        sha = run_git(git_repo, "rev-parse", "HEAD")
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", CURRENT_RUN_ID)
        return authorize_module.main(
            [sha, "--repo", "curie-eng/agentos", "--main-ref", "main"]
        )

    def test_gh_api_failure_refuses_with_a_message_naming_the_lookup(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(
            monkeypatch,
            subprocess.CalledProcessError(
                1,
                ["gh", "api", "-X", "GET", "repos/curie-eng/agentos/commits/x/check-runs"],
                stderr="gh: Not Found (HTTP 404)",
            ),
        )

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "ERROR: could not retrieve check-runs" in stderr
        assert "gh: Not Found (HTTP 404)" in stderr

    def test_unparseable_response_refuses_with_a_message_naming_the_lookup(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(
            monkeypatch, json.JSONDecodeError("Expecting value", "not json", 0)
        )

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        assert "ERROR: could not retrieve check-runs" in capsys.readouterr().err

    def test_payload_without_check_runs_key_refuses_with_a_message(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(monkeypatch, KeyError("check_runs"))

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        assert "ERROR: could not retrieve check-runs" in capsys.readouterr().err

    def test_lookup_failure_is_distinguishable_from_an_unauthorized_tag(
        self, git_repo, monkeypatch, capsys
    ):
        # The unauthorized-tag wording must not appear on the lookup path, or
        # an operator cannot tell the two refusals apart.
        self._stub_fetch_raising(monkeypatch, KeyError("check_runs"))

        assert self._run_main(git_repo, monkeypatch) == 1
        lookup_stderr = capsys.readouterr().err

        monkeypatch.setattr(
            authorize_module, "fetch_check_runs", lambda sha, repo: CHECK_RUNS_ONE_FAILED
        )
        assert self._run_main(git_repo, monkeypatch) == 1
        refusal_stderr = capsys.readouterr().err

        assert "could not retrieve check-runs" in lookup_stderr
        assert "could not retrieve check-runs" not in refusal_stderr
        assert "does not have a fully successful set of check-runs" in refusal_stderr
