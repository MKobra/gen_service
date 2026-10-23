import asyncpg


async def assert_balance_matches_ledger(
    connection: asyncpg.Connection,
    user_id: str,
) -> None:
    row = await connection.fetchrow(
        """
        SELECT users.balance, COALESCE(SUM(ledger_entries.delta), 0) AS ledger_balance
        FROM users
        LEFT JOIN ledger_entries ON ledger_entries.user_id = users.user_id
        WHERE users.user_id = $1
        GROUP BY users.user_id, users.balance
        """,
        user_id,
    )

    assert row is not None
    assert row["balance"] == row["ledger_balance"]


async def read_journal(
    connection: asyncpg.Connection,
    user_id: str,
) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        SELECT *
        FROM ledger_entries
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user_id,
    )
