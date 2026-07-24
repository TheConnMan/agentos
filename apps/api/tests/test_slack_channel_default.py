"""The CLI's default Slack channel must satisfy the API's channel-ID validation.

Regression guard for #341: the CLI shipped `#local-dev` as its default channel,
which the API's own AgentCreate validator rejects with 422, so
`curie local deploy` with no --slack-channel failed on a fresh stack. This test
reads the real Rust const and runs it through the real API validator so the two
literals cannot drift back apart across the language boundary.
"""

import re
from pathlib import Path

from curie_api.schemas import _validate_slack_channel_id

_API_RS = Path(__file__).resolve().parents[3] / "cli" / "src" / "api.rs"
_DEFAULT_RE = re.compile(r'DEFAULT_SLACK_CHANNEL:\s*&str\s*=\s*"([^"]*)"')


def _cli_default_channel() -> str:
    match = _DEFAULT_RE.search(_API_RS.read_text(encoding="utf-8"))
    assert match, f"could not find DEFAULT_SLACK_CHANNEL in {_API_RS}"
    return match.group(1)


def test_cli_default_channel_passes_api_validation() -> None:
    value = _cli_default_channel()
    # Validator raises on a bad shape and echoes the value back on success.
    assert _validate_slack_channel_id(value) == value
