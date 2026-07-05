import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlite3

import pytest

from app.db import ensure_db, execute_readonly, get_schema_text


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "shop.db"
    ensure_db(path)
    return path


def test_seeding_is_deterministic(db_path, tmp_path):
    other = tmp_path / "shop2.db"
    ensure_db(other)
    for table in ("customers", "products", "orders", "order_items"):
        a = execute_readonly(db_path, f"SELECT * FROM {table} ORDER BY 1, 2")
        b = execute_readonly(other, f"SELECT * FROM {table} ORDER BY 1, 2")
        assert a == b


def test_seeded_row_counts(db_path):
    _, rows, _ = execute_readonly(
        db_path,
        "SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM products), "
        "(SELECT COUNT(*) FROM orders), (SELECT COUNT(*) FROM order_items)",
    )
    customers, products, orders, order_items = rows[0]
    assert (customers, products, orders) == (10, 12, 40)
    assert order_items == 80  # 1 + (i % 3) items per order, i = 1..40


def test_ensure_db_is_idempotent(db_path):
    before = execute_readonly(db_path, "SELECT COUNT(*) FROM orders")
    ensure_db(db_path)  # second call must not reseed or duplicate
    assert execute_readonly(db_path, "SELECT COUNT(*) FROM orders") == before


def test_schema_text_contains_all_tables_and_samples(db_path):
    schema = get_schema_text(db_path)
    for table in ("customers", "products", "orders", "order_items"):
        assert f"CREATE TABLE {table}" in schema
    assert "-- sample rows from customers:" in schema


def test_join_query_executes(db_path):
    columns, rows, truncated = execute_readonly(
        db_path,
        """
        SELECT p.name, SUM(oi.quantity * oi.unit_price) AS revenue
        FROM order_items oi JOIN products p ON p.id = oi.product_id
        GROUP BY p.name ORDER BY revenue DESC LIMIT 3
        """,
    )
    assert columns == ["name", "revenue"]
    assert len(rows) == 3
    assert not truncated
    # descending revenue
    assert rows[0][1] >= rows[1][1] >= rows[2][1]


def test_row_cap_sets_truncated_flag(db_path):
    _, rows, truncated = execute_readonly(db_path, "SELECT * FROM order_items", max_rows=10)
    assert len(rows) == 10
    assert truncated


def test_readonly_connection_cannot_write_even_without_authorizer(db_path):
    # Belt half of belt-and-braces: mode=ro alone blocks writes.
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM orders")
    conn.close()
