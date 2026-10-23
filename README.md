# gen_service

## Local development

Start PostgreSQL and the four-worker API:

```bash
docker compose up --build
```

Compose waits for PostgreSQL, applies `alembic upgrade head` once, then starts the API workers. To create the next migration, run:

```bash
docker compose run --rm migrate alembic -c pyproject.toml revision -m "describe the schema change"
```

The migration service applies new revisions on the next `docker compose up`.

Install the test tools and run the health check from another terminal:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

The test client defaults to `http://localhost:8000`. Set `SERVICE_BASE_URL` to override it. Tests that inspect database state connect to the Compose PostgreSQL service at `localhost:5432` by default; set `TEST_DATABASE_URL` to override that connection.

Run the configured static checks with:

```bash
ruff check .
mypy app
```
