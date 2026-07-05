"""
NL2SQL evaluation harness — execution accuracy.

--dry-run (offline, no API key): every ground-truth query must pass
validation and execute against the seeded DB. This catches dataset rot
(schema drift, typos) without spending tokens.

Full mode (needs ANTHROPIC_API_KEY): for each question, generate SQL
with the real generator, execute both generated and ground-truth SQL,
and compare result sets order-insensitively (sorted tuples of
stringified cells) — the model may pick different orderings or column
names, but the *data* must match. Reports:

  validity rate       generated SQL that passed validation + executed
  execution accuracy  generated results that match ground truth

--no-repair disables the one-shot self-correction the API applies, so
running with and without it measures what the repair loop is worth.

Usage (from the project root):
    python eval/run_eval.py --dry-run
    python eval/run_eval.py
    python eval/run_eval.py --no-repair
"""
from __future__ import annotations
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import QueryExecutionError, ensure_db, execute_readonly, get_schema_text
from app.validation import check_sql, normalize_sql

EVAL_DIR = Path(__file__).resolve().parent


def result_signature(rows: list) -> list:
    """Order-insensitive, type-tolerant view of a result set. Floats are
    rounded before stringification so 3320.0 == 3319.9999999999995."""
    def cell(value):
        if isinstance(value, float):
            value = round(value, 6)
        return str(value)
    return sorted(tuple(cell(v) for v in row) for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="only validate + execute the ground-truth SQL (offline)")
    parser.add_argument("--no-repair", action="store_true",
                        help="disable the one-shot self-correction retry")
    args = parser.parse_args()

    dataset = json.loads((EVAL_DIR / "dataset.json").read_text(encoding="utf-8"))
    items = dataset["items"]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "shop.db"
        ensure_db(db_path)

        failures = 0
        for item in items:
            sql = normalize_sql(item["ground_truth_sql"])
            ok, reason = check_sql(sql)
            if not ok:
                print(f"BAD GROUND TRUTH (validation): {item['question']} — {reason}")
                failures += 1
                continue
            try:
                execute_readonly(db_path, sql)
            except QueryExecutionError as exc:
                print(f"BAD GROUND TRUTH (execution): {item['question']} — {exc}")
                failures += 1
        print(f"Ground truth: {len(items) - failures}/{len(items)} queries valid and executable")
        if failures:
            return 1
        if args.dry_run:
            return 0

        return run_generation_eval(db_path, items, repair=not args.no_repair)


def try_execute(db_path: Path, sql: str):
    """Returns (rows, error). error is None on success."""
    ok, reason = check_sql(sql)
    if not ok:
        return None, reason
    try:
        _, rows, _ = execute_readonly(db_path, sql)
        return rows, None
    except QueryExecutionError as exc:
        return None, str(exc)


def run_generation_eval(db_path: Path, items: list, repair: bool = True) -> int:
    from app.sql_generation import ClaudeSqlGenerator

    schema_text = get_schema_text(db_path)
    generator = ClaudeSqlGenerator()

    valid = correct = repairs_used = 0
    print(f"\nself-correction: {'on' if repair else 'off'}")
    print(f"{'ok':>8}  question")
    print("-" * 72)
    for item in items:
        sql = normalize_sql(generator.generate(item["question"], schema_text))
        rows, error = try_execute(db_path, sql)
        if error is not None and repair:
            sql = normalize_sql(generator.repair(item["question"], schema_text, sql, error))
            rows, error = try_execute(db_path, sql)
            if error is None:
                repairs_used += 1

        if error is not None:
            outcome = "FAIL"
        else:
            valid += 1
            _, expected_rows, _ = execute_readonly(db_path, item["ground_truth_sql"])
            if result_signature(rows) == result_signature(expected_rows):
                correct += 1
                outcome = "ok"
            else:
                outcome = "WRONG"
        print(f"{outcome:>8}  {item['question']}")
        if outcome != "ok":
            print(f"          generated: {sql}")

    n = len(items)
    print("-" * 72)
    print(f"validity rate:      {valid}/{n} generated queries validated and executed")
    print(f"execution accuracy: {correct}/{n} matched ground-truth results")
    if repair:
        print(f"repairs used:       {repairs_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
