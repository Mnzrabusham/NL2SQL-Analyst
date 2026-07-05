"""
Pre-execution SQL checks.

The security boundary is the read-only connection and default-deny
authorizer in db.py, enforced inside the SQLite engine. These checks
exist to fail fast with a readable reason: an HTTP 400 saying "only
single SELECT statements are allowed" is more useful to a caller than
the engine's generic "not authorized".
"""
from __future__ import annotations
import re
from typing import Tuple


def normalize_sql(sql: str) -> str:
    """Trim whitespace and at most one trailing semicolon."""
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def _strip_comments_and_strings(sql: str) -> str:
    """Remove string literals and comments so structural checks (like the
    single-statement rule) can't be fooled by semicolons inside them."""
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def check_sql(sql: str) -> Tuple[bool, str]:
    """Return (ok, reason). Reason is empty when ok."""
    normalized = normalize_sql(sql)
    if not normalized:
        return False, "Empty SQL statement."

    structural = _strip_comments_and_strings(normalized)
    if ";" in structural:
        return False, "Only a single SQL statement is allowed."

    first_word_match = re.match(r"\s*([A-Za-z]+)", structural)
    first_word = first_word_match.group(1).upper() if first_word_match else ""
    if first_word not in ("SELECT", "WITH"):
        return False, f"Only SELECT queries are allowed (statement starts with {first_word or 'nothing recognizable'})."

    return True, ""
