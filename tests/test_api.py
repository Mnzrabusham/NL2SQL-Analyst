import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sql_generation import SqlGenerator, strip_code_fences


class FakeGenerator(SqlGenerator):
    """Returns canned SQL so endpoint tests run offline."""

    def __init__(self, sql: str):
        self.sql = sql

    def generate(self, question: str, schema_text: str) -> str:
        return self.sql


class RepairingFakeGenerator(SqlGenerator):
    """Bad SQL on the first attempt, a fixed statement from repair()."""

    def __init__(self, first_sql: str, repaired_sql: str):
        self.first_sql = first_sql
        self.repaired_sql = repaired_sql
        self.repair_calls = []

    def generate(self, question: str, schema_text: str) -> str:
        return self.first_sql

    def repair(self, question, schema_text, failed_sql, error) -> str:
        self.repair_calls.append((failed_sql, error))
        return self.repaired_sql


def make_client(tmp_path, sql: str) -> TestClient:
    settings = Settings(db_path=tmp_path / "shop.db")
    return TestClient(create_app(settings=settings, generator=FakeGenerator(sql)))


def test_health(tmp_path):
    client = make_client(tmp_path, "SELECT 1")
    assert client.get("/health").json() == {"status": "ok"}


def test_schema_endpoint(tmp_path):
    client = make_client(tmp_path, "SELECT 1")
    schema = client.get("/schema").json()["schema_text"]
    assert "CREATE TABLE customers" in schema


def test_ask_happy_path(tmp_path):
    client = make_client(tmp_path, "SELECT COUNT(*) AS n FROM customers")
    resp = client.post("/ask", json={"question": "how many customers are there?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sql"] == "SELECT COUNT(*) AS n FROM customers"
    assert body["columns"] == ["n"]
    assert body["rows"] == [[10]]
    assert body["truncated"] is False
    assert body["repaired"] is False


def test_ask_self_correction_recovers_from_bad_sql(tmp_path):
    generator = RepairingFakeGenerator(
        first_sql="SELECT COUNT(*) FROM cusstomers",  # typo'd table
        repaired_sql="SELECT COUNT(*) AS n FROM customers",
    )
    settings = Settings(db_path=tmp_path / "shop.db")
    client = TestClient(create_app(settings=settings, generator=generator))

    resp = client.post("/ask", json={"question": "how many customers?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["repaired"] is True
    assert body["rows"] == [[10]]
    # The repair prompt received the failed SQL and the engine's error.
    (failed_sql, error), = generator.repair_calls
    assert "cusstomers" in failed_sql
    assert error


def test_ask_self_correction_can_fix_non_select(tmp_path):
    generator = RepairingFakeGenerator(
        first_sql="DELETE FROM orders",
        repaired_sql="SELECT COUNT(*) AS n FROM orders",
    )
    settings = Settings(db_path=tmp_path / "shop.db")
    client = TestClient(create_app(settings=settings, generator=generator))

    resp = client.post("/ask", json={"question": "delete all orders"})
    assert resp.status_code == 200
    assert resp.json()["repaired"] is True


def test_ask_repair_that_also_fails_returns_400(tmp_path):
    generator = RepairingFakeGenerator(
        first_sql="SELECT * FROM nonexistent_table",
        repaired_sql="SELECT * FROM still_nonexistent",
    )
    settings = Settings(db_path=tmp_path / "shop.db")
    client = TestClient(create_app(settings=settings, generator=generator))

    resp = client.post("/ask", json={"question": "gibberish"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["sql"] == "SELECT * FROM still_nonexistent"


def test_ask_rejected_sql_returns_400_with_sql(tmp_path):
    client = make_client(tmp_path, "DELETE FROM orders")
    resp = client.post("/ask", json={"question": "delete all orders"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "SELECT" in detail["reason"]
    assert detail["sql"] == "DELETE FROM orders"


def test_ask_engine_rejection_returns_400(tmp_path):
    # Passes the pre-check but the authorizer denies it at prepare time.
    client = make_client(tmp_path, "SELECT * FROM pragma_module_list")
    resp = client.post("/ask", json={"question": "sneaky pragma"})
    assert resp.status_code == 400


def test_ask_bad_syntax_returns_400(tmp_path):
    client = make_client(tmp_path, "SELECT FROM WHERE")
    resp = client.post("/ask", json={"question": "gibberish"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["sql"] == "SELECT FROM WHERE"


def test_ask_strips_trailing_semicolon(tmp_path):
    client = make_client(tmp_path, "SELECT COUNT(*) AS n FROM products;")
    resp = client.post("/ask", json={"question": "how many products?"})
    assert resp.status_code == 200
    assert resp.json()["rows"] == [[12]]


def test_strip_code_fences():
    assert strip_code_fences("```sql\nSELECT 1\n```") == "SELECT 1"
    assert strip_code_fences("```\nSELECT 1\n```") == "SELECT 1"
    assert strip_code_fences("SELECT 1") == "SELECT 1"
