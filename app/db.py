"""
Database layer: sample data, schema introspection, and safe execution.

Generated SQL is enforced inside the SQLite engine rather than by
inspecting the SQL string:

  1. The connection is opened read-only (`mode=ro`), so the file cannot
     be written even if the other layers fail.
  2. An authorizer callback runs default-deny: only SELECT, column
     reads, and function calls are permitted. PRAGMA, ATTACH, and all
     writes are denied at prepare time, including variants a string
     check would miss (comment-obfuscated keywords, DML inside CTEs).
  3. A progress handler aborts queries after a fixed instruction budget,
     and results are truncated at a row cap.

Sample data is seeded deterministically in code (no random module, no
committed binary) so that evals comparing query results are reproducible.
"""
from __future__ import annotations
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import List, Tuple

INSTRUCTION_BUDGET_STEPS = 10_000  # progress-handler callback interval
MAX_PROGRESS_CALLBACKS = 500       # ~5M VM instructions before abort


class QueryExecutionError(Exception):
    """The SQL was rejected or failed inside the engine (authorizer denial,
    syntax error, aborted runaway query). Safe to show to the caller."""


SCHEMA_SQL = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    country     TEXT NOT NULL,
    signup_date TEXT NOT NULL
);
CREATE TABLE products (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE order_items (
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
"""

CUSTOMERS = [
    (1, "Ada Lindqvist", "ada@example.com", "Sweden", "2025-11-03"),
    (2, "Bruno Costa", "bruno@example.com", "Portugal", "2025-11-17"),
    (3, "Chidi Okafor", "chidi@example.com", "Nigeria", "2025-12-01"),
    (4, "Dana Whitfield", "dana@example.com", "United States", "2025-12-14"),
    (5, "Emre Yilmaz", "emre@example.com", "Turkey", "2026-01-05"),
    (6, "Farah Haddad", "farah@example.com", "Jordan", "2026-01-22"),
    (7, "Grace Kim", "grace@example.com", "South Korea", "2026-02-08"),
    (8, "Henrik Voss", "henrik@example.com", "Germany", "2026-02-19"),
    (9, "Isabela Duarte", "isabela@example.com", "Brazil", "2026-03-02"),
    (10, "Jonas Meyer", "jonas@example.com", "Germany", "2026-03-15"),
]

PRODUCTS = [
    (1, "Aluminum Laptop Stand", "Accessories", 49.00),
    (2, "Mechanical Keyboard", "Peripherals", 129.00),
    (3, "4K Webcam", "Peripherals", 179.00),
    (4, "USB-C Dock", "Accessories", 89.00),
    (5, "Noise-Cancelling Headset", "Audio", 249.00),
    (6, "Ergonomic Mouse", "Peripherals", 69.00),
    (7, "27-inch Monitor", "Displays", 329.00),
    (8, "Desk Mat", "Accessories", 25.00),
    (9, "Ring Light", "Studio", 59.00),
    (10, "Condenser Microphone", "Audio", 149.00),
    (11, "Standing Desk Converter", "Furniture", 279.00),
    (12, "Cable Organizer Kit", "Accessories", 19.00),
]

ORDER_STATUSES = ["shipped", "shipped", "shipped", "pending", "shipped", "returned"]


def _seed_orders() -> tuple[list, list]:
    """Deterministic orders/items: fixed arithmetic, no randomness, so
    row counts and aggregates are stable for tests and evals."""
    orders, items = [], []
    product_price = {pid: price for pid, _, _, price in PRODUCTS}
    for i in range(1, 41):
        customer_id = ((i * 7) % 10) + 1
        month = (i % 6) + 1
        day = ((i * 3) % 28) + 1
        status = ORDER_STATUSES[i % len(ORDER_STATUSES)]
        orders.append((i, customer_id, f"2026-{month:02d}-{day:02d}", status))
        for j in range(1 + (i % 3)):
            product_id = ((i * 5 + j * 3) % 12) + 1
            quantity = ((i + j) % 4) + 1
            items.append((i, product_id, quantity, product_price[product_id]))
    return orders, items


def ensure_db(db_path: Path) -> None:
    """Create and seed the sample database if it doesn't exist yet."""
    db_path = Path(db_path)
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    orders, items = _seed_orders()
    # closing() matters: `with connect()` alone manages the transaction,
    # not the connection, and an open handle keeps the file locked on Windows.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(SCHEMA_SQL)
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", CUSTOMERS)
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", orders)
        conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?)", items)


def get_schema_text(db_path: Path) -> str:
    """DDL plus a few sample rows per table — the sample rows cost little
    and measurably improve the model's SQL (formats, value conventions)."""
    with closing(sqlite3.connect(db_path)) as conn:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        parts = []
        for name, ddl in tables:
            parts.append(ddl.strip() + ";")
            rows = conn.execute(f"SELECT * FROM {name} LIMIT 3").fetchall()
            samples = "\n".join(f"--   {row}" for row in rows)
            parts.append(f"-- sample rows from {name}:\n{samples}")
    return "\n\n".join(parts)


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    # as_posix(): backslash paths break the file: URI scheme on Windows.
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)

    def authorize(action, arg1, arg2, db_name, trigger) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(authorize)

    calls = {"n": 0}

    def abort_when_over_budget() -> int:
        calls["n"] += 1
        return 1 if calls["n"] > MAX_PROGRESS_CALLBACKS else 0

    conn.set_progress_handler(abort_when_over_budget, INSTRUCTION_BUDGET_STEPS)
    return conn


def execute_readonly(
    db_path: Path, sql: str, max_rows: int = 200
) -> Tuple[List[str], List[list], bool]:
    """Run a validated SELECT and return (columns, rows, truncated)."""
    conn = _readonly_connection(db_path)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in cursor.description] if cursor.description else []
    except sqlite3.Error as exc:
        raise QueryExecutionError(str(exc)) from exc
    finally:
        conn.close()

    truncated = len(rows) > max_rows
    return columns, [list(r) for r in rows[:max_rows]], truncated
