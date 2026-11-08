"""Add isolated LEGACY family people and relationship graph tables."""

from alembic import op
import sqlalchemy as sa


revision = "0006_family_graph"
down_revision = "0005_secure_catalogue"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "family_people",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("death_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["family_rooms.id"], ondelete="CASCADE",
            name="fk_family_people_room_id_family_rooms",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_family_people_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_family_people"),
        sa.UniqueConstraint("room_id", "id", name="uq_family_people_room_id"),
        sa.UniqueConstraint("room_id", "user_id", name="uq_family_people_room_user"),
    )
    op.create_table(
        "family_relationships",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("from_person_id", sa.String(), nullable=False),
        sa.Column("to_person_id", sa.String(), nullable=False),
        sa.Column("relation_type", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["family_rooms.id"], ondelete="CASCADE",
            name="fk_family_relationships_room_id_family_rooms",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "from_person_id"],
            ["family_people.room_id", "family_people.id"],
            ondelete="CASCADE",
            name="fk_family_relationships_from_person",
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "to_person_id"],
            ["family_people.room_id", "family_people.id"],
            ondelete="CASCADE",
            name="fk_family_relationships_to_person",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_family_relationships"),
        sa.UniqueConstraint(
            "room_id", "from_person_id", "to_person_id", "relation_type",
            name="uq_family_relationship_edge",
        ),
        sa.CheckConstraint(
            "from_person_id != to_person_id",
            name="ck_family_relationship_not_self",
        ),
        sa.CheckConstraint(
            "relation_type IN ('PARENT_OF', 'SPOUSE_OF')",
            name="ck_family_relationship_type",
        ),
        sa.CheckConstraint(
            "relation_type != 'SPOUSE_OF' OR from_person_id < to_person_id",
            name="ck_family_relationship_spouse_order",
        ),
    )


def downgrade():
    bind = op.get_bind()
    if any(
        bind.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first() is not None
        for table in ("family_relationships", "family_people")
    ):
        raise RuntimeError("refusing downgrade: family graph data would be lost")
    op.drop_table("family_relationships")
    op.drop_table("family_people")

