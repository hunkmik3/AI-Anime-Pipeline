"""every episode belongs to a series

`scene.series_id` was nullable to let rows predating the Series tier survive
their own migration. `create_scene` has always put an episode with no series
into the project's "Default" one, so nothing in the app produced an orphan — but
the column still allowed it, which makes the four-tier hierarchy a habit rather
than a rule.

Backfills before tightening, and does it defensively: this runs against a
production database that has been through several restructures, and an
`ALTER COLUMN SET NOT NULL` that meets one stray row fails the whole deploy.
Anything without a series is adopted by its project's first series, and a
project with no series at all gets one.

Revision ID: 750b89b7603b
Revises: f0e7c4c2c382
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "750b89b7603b"
down_revision: Union[str, Sequence[str], None] = "f0e7c4c2c382"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    orphans = conn.execute(
        sa.text("SELECT count(*) FROM scene WHERE series_id IS NULL")
    ).scalar_one()

    if orphans:
        # A project holding orphans but no series needs one to adopt them into.
        conn.execute(
            sa.text(
                """
                INSERT INTO series (id, project_id, name, code, unit_label,
                                    order_index)
                SELECT gen_random_uuid(), p.id, 'Default', '', 'Episode', 0
                FROM project p
                WHERE EXISTS (
                          SELECT 1 FROM scene s
                          WHERE s.project_id = p.id AND s.series_id IS NULL
                      )
                  AND NOT EXISTS (
                          SELECT 1 FROM series x WHERE x.project_id = p.id
                      )
                """
            )
        )
        # Adopt into the project's FIRST series — the one a person would call
        # "the" series of that project, rather than an arbitrary row.
        conn.execute(
            sa.text(
                """
                UPDATE scene s
                SET series_id = (
                    SELECT x.id FROM series x
                    WHERE x.project_id = s.project_id
                    ORDER BY x.order_index, x.id
                    LIMIT 1
                )
                WHERE s.series_id IS NULL
                """
            )
        )

    left = conn.execute(
        sa.text("SELECT count(*) FROM scene WHERE series_id IS NULL")
    ).scalar_one()
    if left:
        # Only reachable if a scene points at a project that does not exist, in
        # which case the constraint is not the problem to fix here.
        raise RuntimeError(
            f"{left} scene row(s) still have no series and could not be adopted; "
            "refusing to add the constraint over unexplained data"
        )

    op.alter_column("scene", "series_id", existing_type=sa.Uuid(), nullable=False)


def downgrade() -> None:
    op.alter_column("scene", "series_id", existing_type=sa.Uuid(), nullable=True)
