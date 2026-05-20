"""initial anime schema

Phase 1 greenfield migration: Project -> Scene -> Shot hierarchy replaces
the old Board model. Existing SQLite DB (if any) is archived by the
caller before running this -- we don't carry forward old rows.

Revision ID: 86e7b45a4366
Revises:
Create Date: 2026-05-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "86e7b45a4366"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm -- used later for fuzzy reference/asset library search.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # project
    op.create_table(
        "project",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "project_bible",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # asset (early; scene/shot reference it). node_id FK wired after node exists.
    op.create_table(
        "asset",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("node_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("uuid_media_id", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column(
            "asset_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("uuid_media_id", name="uq_asset_uuid_media_id"),
    )
    op.create_index("ix_asset_project_id", "asset", ["project_id"])
    op.create_index("ix_asset_node_id", "asset", ["node_id"])
    op.create_index("ix_asset_uuid_media_id", "asset", ["uuid_media_id"])

    # scene
    op.create_table(
        "scene",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scene_bible_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "master_establishing_asset_id",
            sa.Integer(),
            sa.ForeignKey("asset.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_scene_project_id", "scene", ["project_id"])
    op.create_index("ix_scene_project_order", "scene", ["project_id", "order_index"])

    # shot. current_node_id FK added later (after node table exists).
    op.create_table(
        "shot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scene_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scene.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("script_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("current_node_id", sa.Integer(), nullable=True),
        sa.Column(
            "final_video_asset_id",
            sa.Integer(),
            sa.ForeignKey("asset.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "workflow_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_shot_scene_id", "shot", ["scene_id"])
    op.create_index("ix_shot_scene_order", "shot", ["scene_id", "order_index"])

    # node
    op.create_table(
        "node",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "shot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("short_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("w", sa.Float(), nullable=False, server_default="240"),
        sa.Column("h", sa.Float(), nullable=False, server_default="160"),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_node_shot_id", "node", ["shot_id"])
    op.create_index("ix_node_short_id", "node", ["short_id"])
    op.create_index("ux_node_shot_short", "node", ["shot_id", "short_id"], unique=True)

    # Wire deferred FKs back to node now that it exists.
    op.create_foreign_key(
        "fk_asset_node_id",
        "asset",
        "node",
        ["node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_shot_current_node_id",
        "shot",
        "node",
        ["current_node_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # edge
    op.create_table(
        "edge",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "shot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.Integer(),
            sa.ForeignKey("node.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default="ref"),
        sa.Column("source_variant_idx", sa.Integer(), nullable=True),
    )
    op.create_index("ix_edge_shot_id", "edge", ["shot_id"])

    # request
    op.create_table(
        "request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            sa.Integer(),
            sa.ForeignKey("node.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_request_node_id", "request", ["node_id"])

    # reference
    op.create_table(
        "reference",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ai_brief", sa.Text(), nullable=True),
        sa.Column("aspect_ratio", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source_shot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shot.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_node_short_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("media_id", name="uq_reference_media_id"),
    )
    op.create_index("ix_reference_project_id", "reference", ["project_id"])
    op.create_index("ix_reference_media_id", "reference", ["media_id"])
    op.create_index("ix_reference_source_shot_id", "reference", ["source_shot_id"])

    # chatmessage
    op.create_table(
        "chatmessage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_chatmessage_project_id", "chatmessage", ["project_id"])

    # plan / planrevision / pipelinerun (transitional; replaced in Phase 7)
    op.create_table(
        "plan",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "shot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_plan_shot_id", "plan", ["shot_id"])

    op.create_table(
        "planrevision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("plan.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rev_no", sa.Integer(), nullable=False),
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "edits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_planrevision_plan_id", "planrevision", ["plan_id"])

    op.create_table(
        "pipelinerun",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("plan.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_pipelinerun_plan_id", "pipelinerun", ["plan_id"])

    # project_flow_mapping (renamed from boardflowproject)
    op.create_table(
        "project_flow_mapping",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("flow_project_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("project_flow_mapping")
    op.drop_index("ix_pipelinerun_plan_id", table_name="pipelinerun")
    op.drop_table("pipelinerun")
    op.drop_index("ix_planrevision_plan_id", table_name="planrevision")
    op.drop_table("planrevision")
    op.drop_index("ix_plan_shot_id", table_name="plan")
    op.drop_table("plan")
    op.drop_index("ix_chatmessage_project_id", table_name="chatmessage")
    op.drop_table("chatmessage")
    op.drop_index("ix_reference_source_shot_id", table_name="reference")
    op.drop_index("ix_reference_media_id", table_name="reference")
    op.drop_index("ix_reference_project_id", table_name="reference")
    op.drop_table("reference")
    op.drop_index("ix_request_node_id", table_name="request")
    op.drop_table("request")
    op.drop_index("ix_edge_shot_id", table_name="edge")
    op.drop_table("edge")
    op.drop_constraint("fk_shot_current_node_id", "shot", type_="foreignkey")
    op.drop_constraint("fk_asset_node_id", "asset", type_="foreignkey")
    op.drop_index("ux_node_shot_short", table_name="node")
    op.drop_index("ix_node_short_id", table_name="node")
    op.drop_index("ix_node_shot_id", table_name="node")
    op.drop_table("node")
    op.drop_index("ix_shot_scene_order", table_name="shot")
    op.drop_index("ix_shot_scene_id", table_name="shot")
    op.drop_table("shot")
    op.drop_index("ix_scene_project_order", table_name="scene")
    op.drop_index("ix_scene_project_id", table_name="scene")
    op.drop_table("scene")
    op.drop_index("ix_asset_uuid_media_id", table_name="asset")
    op.drop_index("ix_asset_node_id", table_name="asset")
    op.drop_index("ix_asset_project_id", table_name="asset")
    op.drop_table("asset")
    op.drop_table("project")
