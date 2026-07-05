import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.db import QueryExecutionError, ensure_db, execute_readonly
from app.validation import check_sql

# Statements that must never execute. Each is rejected either by the
# fast pre-check (clean 400 with a reason) or — the actual security
# boundary — the read-only connection + default-deny authorizer inside
# the SQLite engine.
MALICIOUS = [
    "UPDATE customers SET name = 'pwned'",
    "DELETE FROM orders",
    "DROP TABLE customers",
    "INSERT INTO customers VALUES (99, 'x', 'x', 'x', 'x')",
    "PRAGMA writable_schema = ON",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "SELECT 1; DROP TABLE customers",
    "WITH x AS (SELECT 1) DELETE FROM orders",
    "CREATE TABLE pwned (id INT)",
    "ALTER TABLE customers ADD COLUMN pwned TEXT",
    "REPLACE INTO products VALUES (1, 'x', 'x', 0)",
    "VACUUM",
    "  /* sneaky */ UPDATE products SET unit_price = 0",
    "SELECT 1; -- comment\nDROP TABLE orders",
]


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "shop.db"
    ensure_db(path)
    return path


def table_counts(db_path):
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "products", "orders", "order_items")
        }


@pytest.mark.parametrize("sql", MALICIOUS)
def test_malicious_sql_never_executes(db_path, sql):
    before = table_counts(db_path)

    ok, _reason = check_sql(sql)
    if ok:
        # Slipped past the pre-check — the engine must stop it.
        with pytest.raises(QueryExecutionError):
            execute_readonly(db_path, sql)

    assert table_counts(db_path) == before, "data changed — safety boundary failed"


def test_plain_select_passes_precheck():
    ok, reason = check_sql("SELECT * FROM customers")
    assert ok, reason


def test_cte_select_passes_precheck():
    ok, reason = check_sql("WITH t AS (SELECT id FROM orders) SELECT COUNT(*) FROM t")
    assert ok, reason


def test_trailing_semicolon_is_tolerated():
    ok, reason = check_sql("SELECT 1;")
    assert ok, reason


def test_semicolon_inside_string_literal_is_fine(db_path):
    ok, reason = check_sql("SELECT 'a;b' AS val")
    assert ok, reason
    columns, rows, _ = execute_readonly(db_path, "SELECT 'a;b' AS val")
    assert rows == [["a;b"]]


def test_empty_sql_is_rejected():
    ok, _ = check_sql("   ")
    assert not ok


def test_select_into_is_blocked_by_engine(db_path):
    # sqlite has no SELECT INTO, but if a model emits it the engine
    # must reject it rather than doing something surprising.
    ok, _ = check_sql("SELECT * INTO pwned FROM customers")
    if ok:
        with pytest.raises(QueryExecutionError):
            execute_readonly(db_path, "SELECT * INTO pwned FROM customers")
