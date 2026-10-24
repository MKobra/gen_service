import sqlalchemy as sa
from alembic import op

revision = "0004_provider_attempts"
down_revision = "0003_generation_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("result_url", sa.Text(), nullable=True))
    op.create_table(
        "task_attempts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_task_attempts_number_positive",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'waiting', 'completed', 'failed')",
            name="ck_task_attempts_status_valid",
        ),
        sa.CheckConstraint(
            "error_type IS NULL OR error_type IN "
            "('technical', 'moderation', 'payload')",
            name="ck_task_attempts_error_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_attempts_task_id_tasks",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_task_attempts"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_number",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_id",
            name="uq_task_attempts_provider_external_id",
        ),
    )
    op.create_index(
        "ix_task_attempts_pending",
        "task_attempts",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON task_attempts TO gen_service_app")
    op.execute("GRANT SELECT, UPDATE ON tasks TO gen_service_app")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON task_attempts FROM gen_service_app")
    op.drop_index("ix_task_attempts_pending", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_column("tasks", "result_url")
