"""drop the write-only deployments.bot_identity column (#502)

``bot_identity`` was written by the git-flow engine on every dev/prod push but
read by no production consumer: the worker's binding-resolution SQL never selected
it, and no console/CLI surface rendered it. A write-only column on the API
contract is dead weight, so it is removed along with its ``DeploymentOut`` /
``WebhookResult`` schema fields and the git-flow write path.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.drop_column("deployments", "bot_identity", schema=SCHEMA)


def downgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("bot_identity", sa.String(), nullable=True),
        schema=SCHEMA,
    )
