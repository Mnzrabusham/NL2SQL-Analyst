"""
SQL generation.

`SqlGenerator` is the interface; `ClaudeSqlGenerator` is the only
implementation. Swapping model providers means adding another subclass.

Failures are raised as typed exceptions so the API layer can map them to
HTTP statuses (missing key -> 503, upstream errors -> 502).
"""
from __future__ import annotations
import logging
import os
import re
from abc import ABC, abstractmethod

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You translate natural-language questions about the SQLite database
described below into SQL.

Rules:
- Output exactly one SQLite SELECT statement (WITH ... SELECT is fine).
- Output ONLY the SQL. No prose, no explanations, no code fences.
- Never write, modify, or delete data. Read-only queries only.
- If the question cannot be answered from this schema, output:
  SELECT 'cannot answer from this schema' AS error
"""


class GenerationError(Exception):
    """Base class for generation-layer failures."""


class GenerationConfigError(GenerationError):
    """The service is misconfigured (e.g. no API key) — an operator problem."""


class GenerationUpstreamError(GenerationError):
    """The Anthropic API call failed — a transient/upstream problem."""


class SqlGenerator(ABC):
    @abstractmethod
    def generate(self, question: str, schema_text: str) -> str:
        """Return a single SQL statement answering the question."""

    def repair(self, question: str, schema_text: str, failed_sql: str, error: str) -> str:
        """One-shot correction after a validation/execution failure.

        Default just regenerates; implementations should feed the error
        back to the model, which is what makes the retry worth having.
        """
        return self.generate(question, schema_text)


class ClaudeSqlGenerator(SqlGenerator):
    def __init__(self, model: str = "claude-sonnet-5"):
        self._model = model

    def _get_client(self) -> anthropic.Anthropic:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise GenerationConfigError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env or set it "
                "in the server's environment."
            )
        return anthropic.Anthropic(api_key=api_key)

    def generate(self, question: str, schema_text: str) -> str:
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Database schema:\n\n{schema_text}\n\nQuestion: {question}",
                }],
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            logger.error("Anthropic API call failed: %s", exc)
            raise GenerationUpstreamError(f"Upstream model call failed: {exc}") from exc

        sql = "".join(block.text for block in response.content if block.type == "text")
        return strip_code_fences(sql)

    def repair(self, question: str, schema_text: str, failed_sql: str, error: str) -> str:
        prompt = (
            f"Database schema:\n\n{schema_text}\n\n"
            f"Question: {question}\n\n"
            f"This SQL was rejected:\n{failed_sql}\n\n"
            f"Failure reason: {error}\n\n"
            f"Return a corrected single SQLite SELECT statement. Output only SQL."
        )
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            logger.error("Anthropic repair call failed: %s", exc)
            raise GenerationUpstreamError(f"Upstream model call failed: {exc}") from exc

        sql = "".join(block.text for block in response.content if block.type == "text")
        return strip_code_fences(sql)


def strip_code_fences(sql: str) -> str:
    """The prompt forbids code fences, but strip them defensively —
    a fenced answer should degrade to working SQL, not a syntax error."""
    sql = sql.strip()
    match = re.match(r"^```(?:sql)?\s*(.*?)\s*```$", sql, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else sql
