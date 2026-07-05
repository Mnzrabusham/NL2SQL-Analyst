# NL2SQL Analyst

![CI](https://github.com/Mnzrabusham/nl2sql-analyst/actions/workflows/ci.yml/badge.svg)

FastAPI service that turns plain-English questions into SQL, executes
them read-only against a bundled SQLite sample database (e-commerce:
customers, products, orders, order items), and returns the generated SQL
together with the result rows. The SQL is always part of the response,
so results can be audited instead of trusted blindly.

Built alongside [rag-doc-assistant](https://github.com/Mnzrabusham/rag-doc-assistant),
with the same project structure.

## Architecture

```
Question (natural language)
      │
      ▼
 db.py            — schema introspection: DDL plus a few sample rows,
      │              so the model sees value formats, not just columns
      ▼
 sql_generation.py — Claude generates a single SELECT (SqlGenerator
      │              interface, one Claude-backed implementation)
      ▼
 validation.py    — string pre-checks that produce readable 400s
      │              (single statement, SELECT/WITH only)
      ▼
 db.py            — execution on a locked-down SQLite connection:
      │              read-only mode, default-deny authorizer, row cap,
      │              instruction budget
      │
      │   on failure: the error goes back to the model for one
      │   corrected attempt, which repeats the same checks
      ▼
   SQL + rows (+ a `repaired` flag), returned via FastAPI (main.py)

 eval/            — questions with ground-truth SQL, scored by
                    execution accuracy (see Evaluation)
```

## SQL safety

Generated SQL is untrusted input. Rather than trying to catch dangerous
statements by parsing the SQL string, enforcement happens inside the
SQLite engine:

1. The connection is opened read-only (`mode=ro`), so the database file
   cannot be written.
2. An authorizer callback runs default-deny: only `SELECT`, column
   reads, and function calls are permitted. `PRAGMA`, `ATTACH`, and all
   writes are rejected at prepare time, including variants a string
   check would miss (comment-obfuscated keywords, DML inside CTEs).
3. A progress-handler instruction budget aborts runaway queries, and
   results are capped at `NL2SQL_MAX_ROWS` (default 200) with a
   `truncated` flag.

The checks in `validation.py` exist only to return readable error
messages; they are not the security boundary. `tests/test_validation.py`
runs a list of hostile statements — multi-statement injection, DML in
CTEs, comment obfuscation — and asserts the data is unchanged afterward.

## Self-correction

When generated SQL fails validation or execution, the failed statement
and the error message are sent back to the model for one corrected
attempt, which goes through the same checks. Most generation errors are
shallow (a wrong column name, a dialect slip), and the engine's own
error message is effective feedback, so a single retry recovers most of
them without much added latency. The retry budget is fixed at one.

Responses include `repaired: true` when the retry produced the final
SQL, and the eval has a `--no-repair` flag so the effect of the retry on
execution accuracy can be measured directly.

## Sample data

`db.py` seeds the database in code on first startup, with fixed literals
and no randomness. This keeps eval results reproducible and avoids
committing a binary `.db` file to the repo.

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # fill in ANTHROPIC_API_KEY
uvicorn app.main:app --port 8001
```

The browser demo only talks to this backend; the API key is read
server-side from the environment. Config is env-driven (see
`.env.example`): `NL2SQL_MODEL`, `NL2SQL_ALLOWED_ORIGINS`,
`NL2SQL_DB_PATH`, `NL2SQL_MAX_ROWS`.

The demo UI is served at `http://localhost:8001/` and the OpenAPI docs
at `http://localhost:8001/docs`. Port 8001 leaves 8000 free for
rag-doc-assistant.

### Docker

```bash
docker build -t nl2sql-analyst .
docker run -p 8001:8001 -e ANTHROPIC_API_KEY=your_key_here nl2sql-analyst
```

## Tests

```bash
pytest tests/
```

The suite runs offline: endpoint tests inject a fake generator, and the
hostile-SQL cases need no API at all. A missing API key surfaces as
HTTP 503.

## Evaluation

```bash
python eval/run_eval.py --dry-run    # offline: ground-truth SQL sanity check
python eval/run_eval.py              # execution accuracy (needs API key)
python eval/run_eval.py --no-repair  # same, with self-correction disabled
```

Each dataset item pairs a question with ground-truth SQL. Generated and
expected result sets are compared order-insensitively with stringified
cells, since the model may pick a different ordering or column aliases.
Reported metrics: validity rate (generated SQL that passed validation
and executed) and execution accuracy (results matching ground truth).

## API

**`POST /ask`**
```json
{"question": "What are the top 3 products by revenue?"}
```
Returns `{sql, columns, rows, row_count, truncated, repaired}`. If the
generated SQL fails validation or execution (after the one repair
attempt), the 400 response includes the rejected SQL and the reason.

**`GET /schema`** — the exact schema text the model is prompted with.

## Limitations

- SQLite only; other dialects would need their own execution layer
  (per-role grants and a read replica fill the authorizer's role on a
  server database)
- The full schema is sent in the prompt; very large schemas would need
  schema retrieval instead
- Read-only by design; no write operations
- No auth, rate limiting, or query history
