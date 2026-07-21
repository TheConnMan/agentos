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
