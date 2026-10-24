import sqlalchemy as sa
from alembic import op

revision = "0005_provider_timeouts"
down_revision = "0004_provider_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_attempts",
        sa.Column("provider_timeout_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE task_attempts "
        "SET provider_timeout_at = now() + interval '60 seconds' "
        "WHERE status = 'waiting'"
    )
    op.drop_constraint(
        "ck_task_attempts_status_valid",
        "task_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_task_attempts_status_valid",
        "task_attempts",
        "status IN ('pending', 'dispatching', 'waiting', 'completed', 'failed', "
        "'timed_out', 'late_ignored')",
    )
    op.create_check_constraint(
        "ck_task_attempts_waiting_timeout_set",
        "task_attempts",
        "(status = 'waiting' AND provider_timeout_at IS NOT NULL) OR "
        "(status <> 'waiting' AND provider_timeout_at IS NULL)",
    )
    op.create_index(
        "ix_task_attempts_waiting_timeout",
        "task_attempts",
        ["provider_timeout_at"],
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_attempts_waiting_timeout",
        table_name="task_attempts",
    )
    op.drop_constraint(
        "ck_task_attempts_waiting_timeout_set",
        "task_attempts",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_attempts_status_valid",
        "task_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_task_attempts_status_valid",
        "task_attempts",
        "status IN ('pending', 'dispatching', 'waiting', 'completed', 'failed')",
    )
    op.drop_column("task_attempts", "provider_timeout_at")
