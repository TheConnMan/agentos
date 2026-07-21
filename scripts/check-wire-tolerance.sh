#!/usr/bin/env bash
# _AciModel tolerant-decode gate (issue #625, following #492). Every direct
# ``ClassName.model_validate*(...)`` call site on an _AciModel subclass, across
# the whole repo, must either thread READER_CONTEXT explicitly or be declared
# in tools/wire-tolerance-gate/allowlist.json with a reason. #492 shipped a
# forgotten-context call site four separate times before four independent
# reviewers caught it by hand; this is the local mirror of the CI gate that
# now catches it instead. See tools/wire-tolerance-gate/src for the scan and
# tools/wire-tolerance-gate/tests for the negative control that proves it is
# not vacuous.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv run python -m agentos_wire_tolerance_gate
