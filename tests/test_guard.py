"""Phase 4: every query from the analyst is checked before it runs (US-10)."""

import duckdb
import pytest

from query_cf import guard


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from marts.fct_complaints",
        "SELECT COUNT(*) FROM MARTS.FCT_COMPLAINTS;",
        "select c.company_name, count(*) from marts.fct_complaints as f "
        "join marts.dim_company as c on c.company_key = f.company_key group by 1",
        "with yearly as (select year(date_received) as y from marts.fct_complaints) "
        "select y, count(*) from yearly group by y",
        "select 1 from marts.dim_date union all select 2 from marts.dim_product",
        "select * from (select product_group from marts.dim_product) as t",
    ],
)
def test_read_only_queries_on_the_marts_are_allowed(sql):
    guard.check_sql(sql)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("insert into marts.dim_date values (1)", "not INSERT"),
        ("update marts.dim_company set company_name = 'x'", "not UPDATE"),
        ("delete from marts.fct_complaints", "not DELETE"),
        ("create table marts.copy as select 1", "not CREATE"),
        ("drop table marts.dim_date", "not DROP"),
        ("copy marts.fct_complaints to 'out.csv'", "not COPY"),
        ("attach 'other.duckdb' as other", "not ATTACH"),
        ("pragma database_list", "not PRAGMA"),
        ("set enable_external_access = true", "not SET"),
        ("install httpfs", "not INSTALL"),
        ("load httpfs", "not COMMAND"),
        ("select 1; drop table marts.dim_date", "exactly one statement, not 2"),
        ("select * from marts.complaint_narratives", "marts.complaint_narratives isn't"),
        ("select * from bronze.cfpb_complaints", "bronze.cfpb_complaints isn't"),
        ("select * from quality.quarantine_complaints", "quality.quarantine_complaints isn't"),
        ("select * from information_schema.tables", "information_schema.tables isn't"),
        ("select * from fct_complaints", "fct_complaints isn't an allowed table"),
        ("select * from query_cf.marts.fct_complaints", "without a database"),
        ("select * from read_csv('C:/secrets.csv')", "table functions"),
        ("select * from 'data.csv'", "data.csv isn't"),
        ("select * from query('select 1')", "table functions"),
        ("select * from marts.dim_date, range(10)", "table functions"),
        ("selec count(*) from", "couldn't be parsed"),
    ],
)
def test_anything_else_is_blocked_with_a_reason(sql, reason):
    with pytest.raises(guard.BlockedQuery, match=reason):
        guard.check_sql(sql)


@pytest.fixture
def warehouse(tmp_path):
    db = tmp_path / "tiny.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "create schema marts; create table marts.dim_date as select range as d from range(5)"
    )
    conn.close()
    (tmp_path / "secret.csv").write_text("a\n1\n", encoding="utf-8")
    return db


def test_the_connection_cant_write_read_files_or_change_settings(warehouse, tmp_path):
    conn = guard.connect(warehouse)
    try:
        assert conn.execute("select count(*) from marts.dim_date").fetchone() == (5,)
        with pytest.raises(duckdb.PermissionException):
            conn.execute(f"select * from read_csv('{(tmp_path / 'secret.csv').as_posix()}')")
        with pytest.raises(duckdb.Error):
            conn.execute("set enable_external_access = true")
        with pytest.raises(duckdb.Error):
            conn.execute("create table marts.t as select 1")
    finally:
        conn.close()


def test_a_slow_query_is_stopped(warehouse):
    conn = guard.connect(warehouse)
    try:
        with pytest.raises(guard.QueryTimeout, match="more than 0.2 seconds"):
            guard.execute(conn, "select count(*) from range(100000000000)", timeout=0.2)
        assert guard.execute(conn, "select 1 as one", timeout=5) == (["one"], [(1,)])
    finally:
        conn.close()


def test_at_most_one_row_more_than_the_limit_comes_back(warehouse):
    conn = guard.connect(warehouse)
    try:
        _, rows = guard.execute(conn, "select d from marts.dim_date", timeout=5, max_rows=2)
        assert len(rows) == 3
    finally:
        conn.close()
