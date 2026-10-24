import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_generation_intake"
down_revision = "0002_append_only_app_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("slug", sa.Text(collation="C"), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("price > 0", name="ck_models_price_positive"),
        sa.PrimaryKeyConstraint("slug", name="pk_models"),
    )
    op.create_table(
        "model_legs",
        sa.Column("model_slug", sa.Text(collation="C"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("max_ref_images", sa.Integer(), nullable=False),
        sa.Column(
            "skip_on_moderation",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.CheckConstraint("priority > 0", name="ck_model_legs_priority_positive"),
        sa.CheckConstraint(
            "max_ref_images >= 0",
            name="ck_model_legs_max_ref_images_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["model_slug"],
            ["models.slug"],
            name="fk_model_legs_model_slug_models",
        ),
        sa.PrimaryKeyConstraint(
            "model_slug",
            "priority",
            name="pk_model_legs",
        ),
    )
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(collation="C"), nullable=False),
        sa.Column("model_slug", sa.Text(collation="C"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "ref_images",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'processing'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("price > 0", name="ck_tasks_price_positive"),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_tasks_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["model_slug"],
            ["models.slug"],
            name="fk_tasks_model_slug_models",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_tasks_user_id_users",
        ),
        sa.PrimaryKeyConstraint("task_id", name="pk_tasks"),
    )

    models = sa.table(
        "models",
        sa.column("slug", sa.Text()),
        sa.column("price", sa.BigInteger()),
    )
    op.bulk_insert(
        models,
        [
            {"slug": "image-fast", "price": 2},
            {"slug": "video-long", "price": 25},
        ],
    )
    model_legs = sa.table(
        "model_legs",
        sa.column("model_slug", sa.Text()),
        sa.column("priority", sa.Integer()),
        sa.column("provider", sa.Text()),
        sa.column("max_ref_images", sa.Integer()),
        sa.column("skip_on_moderation", sa.Boolean()),
    )
    op.bulk_insert(
        model_legs,
        [
            {
                "model_slug": "image-fast",
                "priority": 1,
                "provider": "alpha",
                "max_ref_images": 4,
                "skip_on_moderation": False,
            },
            {
                "model_slug": "image-fast",
                "priority": 2,
                "provider": "beta",
                "max_ref_images": 10,
                "skip_on_moderation": False,
            },
            {
                "model_slug": "video-long",
                "priority": 1,
                "provider": "beta",
                "max_ref_images": 2,
                "skip_on_moderation": False,
            },
            {
                "model_slug": "video-long",
                "priority": 2,
                "provider": "alpha",
                "max_ref_images": 1,
                "skip_on_moderation": True,
            },
        ],
    )

    op.add_column("ledger_entries", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ledger_entries_task_id_tasks",
        "ledger_entries",
        "tasks",
        ["task_id"],
        ["task_id"],
    )
    op.create_index(
        "uq_ledger_entries_one_debit_per_task",
        "ledger_entries",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL AND operation = 'debit'"),
    )

    op.execute("GRANT SELECT ON models, model_legs TO gen_service_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tasks TO gen_service_app")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON tasks FROM gen_service_app")
    op.execute("REVOKE ALL PRIVILEGES ON model_legs FROM gen_service_app")
    op.execute("REVOKE ALL PRIVILEGES ON models FROM gen_service_app")
    op.drop_index(
        "uq_ledger_entries_one_debit_per_task",
        table_name="ledger_entries",
    )
    op.drop_constraint(
        "fk_ledger_entries_task_id_tasks",
        "ledger_entries",
        type_="foreignkey",
    )
    op.drop_column("ledger_entries", "task_id")
    op.drop_table("tasks")
    op.drop_table("model_legs")
    op.drop_table("models")
