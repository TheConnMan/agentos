"""add console_sessions (CLI-minted login codes, #630 / ADR-0049)

The console's credential becomes a server-managed, revocable session cookie
established by exchanging a single-use login code the CLI mints under the
platform key. This table is the session store: one row per grant, holding only
the SHA-256 digests of the login code and the session token, both expiries, and
the consumed/revoked markers.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.create_table(
        "console_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("login_code_hash", sa.String(), nullable=False),
        sa.Column(
            "login_code_expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_token_hash", sa.String(), nullable=True),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    # Unique so a lookup by digest is the only read either credential needs, and
    # a collision is a database error rather than an ambiguous match.
    op.create_index(
        "ix_console_sessions_login_code_hash",
        "console_sessions",
        ["login_code_hash"],
        unique=True,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_console_sessions_session_token_hash",
        "console_sessions",
        ["session_token_hash"],
        unique=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_console_sessions_session_token_hash", "console_sessions", schema=SCHEMA
    )
    op.drop_index(
        "ix_console_sessions_login_code_hash", "console_sessions", schema=SCHEMA
    )
    op.drop_table("console_sessions", schema=SCHEMA)
