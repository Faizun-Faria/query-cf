"""Phase 4: the checks every query from the AI analyst passes before it runs.

Three layers, so no single mistake lets a query change data or read outside the marts:

1. `check_sql` parses the SQL with sqlglot and allows exactly one read-only query that
   reads only the allowlisted mart tables (D-006, US-10).
2. `connect` opens the warehouse read-only, with file and network access turned off and the
   settings locked, so even a query the parser misjudged can't read a file or attach a
   database.
3. `execute` stops a query after a time limit and returns at most a set number of rows.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# Narratives (marts.complaint_narratives) are left out until phase 7 (D-033).
ALLOWED_TABLES = frozenset(
    {
        ("marts", "fct_complaints"),
        ("marts", "dim_company"),
        ("marts", "dim_product"),
        ("marts", "dim_date"),
    }
)
QUERY_ROOTS = (exp.Select, exp.SetOperation)
WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Command,
    exp.Pragma,
    exp.Set,
    exp.Attach,
    exp.Detach,
    exp.Install,
)

# sqlglot logs a warning for statements it can only read as raw commands; those are blocked.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class BlockedQuery(ValueError):
    """The SQL isn't one read-only query on the allowed tables. The message says why."""


class QueryTimeout(RuntimeError):
    """The query ran past the time limit and was stopped."""


def allowed_tables() -> str:
    return ", ".join(f"{schema}.{table}" for schema, table in sorted(ALLOWED_TABLES))


def check_sql(sql: str) -> exp.Expression:
    """Return the parsed query, or raise BlockedQuery saying what isn't allowed."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except SqlglotError as exc:
        raise BlockedQuery(f"the SQL couldn't be parsed: {str(exc).splitlines()[0]}") from None
    if len(statements) != 1:
        raise BlockedQuery(f"send exactly one statement, not {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, QUERY_ROOTS):
        raise BlockedQuery(f"only SELECT queries may run, not {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, WRITES):
            raise BlockedQuery(f"{node.key.upper()} isn't allowed inside a query")

    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise BlockedQuery(
                f"table functions such as {table.this.sql('duckdb')} aren't allowed; "
                f"read only {allowed_tables()}"
            )
        name, schema = table.name.lower(), table.db.lower()
        if table.catalog:
            raise BlockedQuery(f"name tables as schema.table, without a database: {table.sql()}")
        if not schema and name in ctes:
            continue
        if (schema, name) not in ALLOWED_TABLES:
            shown = f"{schema}.{name}" if schema else name
            raise BlockedQuery(f"{shown} isn't an allowed table; read only {allowed_tables()}")
    return tree


def connect(db: str | Path, memory_limit: str | None = None) -> duckdb.DuckDBPyConnection:
    """The warehouse, read-only, with file and network access off and settings locked.

    A memory limit, such as "1GB", keeps a small host such as the public demo's from running
    out; it can't be changed afterwards, since the settings are locked.
    """
    config = {"enable_external_access": False, "lock_configuration": True}
    if memory_limit:
        config["memory_limit"] = memory_limit
    return duckdb.connect(str(db), read_only=True, config=config)


def execute(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list | None = None,
    *,
    timeout: float,
    max_rows: int | None = None,
) -> tuple[list[str], list[tuple]]:
    """Run SQL, stopping it after `timeout` seconds. Returns (columns, rows).

    With max_rows, at most max_rows + 1 rows come back, so the caller can tell the
    result was cut short.
    """
    timer = threading.Timer(timeout, conn.interrupt)
    timer.start()
    try:
        cursor = conn.execute(sql, params) if params else conn.execute(sql)
        rows = cursor.fetchall() if max_rows is None else cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in cursor.description]
    except duckdb.InterruptException:
        raise QueryTimeout(
            f"the query ran for more than {timeout:g} seconds and was stopped"
        ) from None
    finally:
        timer.cancel()
    return columns, rows
