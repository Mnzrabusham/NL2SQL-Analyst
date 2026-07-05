"""
Runtime configuration, read once from the environment.

Same pattern as the companion rag-doc-assistant project: everything an
operator might change between environments lives here, with
development-friendly defaults overridable via env vars (see .env.example).
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class Settings:
    model: str = field(default_factory=lambda: os.getenv("NL2SQL_MODEL", "claude-sonnet-5"))
    # Comma-separated list; "*" is fine for local dev, tighten for real deployments.
    allowed_origins: List[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv("NL2SQL_ALLOWED_ORIGINS", "*").split(",")
            if origin.strip()
        ]
    )
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("NL2SQL_DB_PATH", "data/shop.db"))
    )
    max_rows: int = field(
        default_factory=lambda: int(os.getenv("NL2SQL_MAX_ROWS", "200"))
    )
