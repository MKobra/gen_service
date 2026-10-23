import sqlalchemy as sa
from alembic import op

revision = "0001_payment_ledger"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Text(collation="C"), nullable=False),
        sa.Column(
            "balance",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint("balance >= 0", name="ck_users_balance_nonnegative"),
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
    )
    op.create_table(
        "payments",
        sa.Column("payment_id", sa.Text(collation="C"), nullable=False),
        sa.Column("user_id", sa.Text(collation="C"), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("tokens > 0", name="ck_payments_tokens_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_payments_user_id_users",
        ),
        sa.PrimaryKeyConstraint("payment_id", name="pk_payments"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(collation="C"), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("payment_id", sa.Text(collation="C"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("delta <> 0", name="ck_ledger_entries_delta_nonzero"),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.payment_id"],
            name="fk_ledger_entries_payment_id_payments",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_ledger_entries_user_id_users",
        ),
        sa.PrimaryKeyConstraint("entry_id", name="pk_ledger_entries"),
        sa.UniqueConstraint("payment_id", name="uq_ledger_entries_payment_id"),
    )
    op.create_index(
        "ix_ledger_entries_user_created_at",
        "ledger_entries",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_user_created_at", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("payments")
    op.drop_table("users")
