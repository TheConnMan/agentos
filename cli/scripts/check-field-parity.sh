#!/usr/bin/env bash
# Field-parity gate (#691): every `Deserialize` struct in cli/src/api.rs must
# be declared in cli/api-mirrors.json (as a mirror or a non_mirror), and each
# declared mirror struct must cover its API model's fields per
# cli/api-mirrors.json's allowlisted omissions. See cli/tests/api_field_parity.rs.
set -euo pipefail

cargo test --manifest-path cli/Cargo.toml --test api_field_parity -- --nocapture
