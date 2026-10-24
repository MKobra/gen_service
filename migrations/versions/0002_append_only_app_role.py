from alembic import op

revision = "0002_append_only_app_role"
down_revision = "0001_payment_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE ROLE gen_service_app LOGIN PASSWORD 'gen_service_app' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE"
    )
    op.execute("GRANT CONNECT ON DATABASE gen_service TO gen_service_app")
    op.execute("GRANT USAGE ON SCHEMA public TO gen_service_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON users TO gen_service_app")
    op.execute("GRANT SELECT, INSERT ON payments TO gen_service_app")
    op.execute("GRANT SELECT, INSERT ON ledger_entries TO gen_service_app")


def downgrade() -> None:
    op.execute(
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM gen_service_app"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM gen_service_app"
    )
    op.execute("REVOKE CONNECT ON DATABASE gen_service FROM gen_service_app")
    op.execute("DROP ROLE gen_service_app")
