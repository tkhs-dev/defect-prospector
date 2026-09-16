from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models.record import BugRecord

_DDL = """
CREATE TABLE IF NOT EXISTS bug_records (
    id           TEXT PRIMARY KEY,
    tracker_type TEXT,
    language     TEXT,
    severity     TEXT,
    collected_at TEXT NOT NULL,
    record_json  TEXT NOT NULL
)
"""


class Storage:

    def __init__(self, dataset_dir: Path) -> None:
        dataset_dir.mkdir(parents=True, exist_ok=True)
        self._db = dataset_dir / "dataset.db"
        with self._connect() as conn:
            conn.execute(_DDL)

    def save(self, record: BugRecord) -> None:
        data = json.dumps(asdict(record), default=_serialize)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bug_records VALUES (?,?,?,?,?,?)",
                (
                    record.id,
                    record.source.tracker_type,
                    record.reproduction.environment.language,
                    record.source.severity,
                    record.metadata.collected_at.isoformat(),
                    data,
                ),
            )

    def exists(self, bug_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM bug_records WHERE id = ?", (bug_id,)
            ).fetchone()
            return row is not None

    def get(self, bug_id: str) -> BugRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM bug_records WHERE id = ?", (bug_id,)
            ).fetchone()
        if row is None:
            return None
        return BugRecord.from_dict(json.loads(row[0]))

    def list_all(self) -> list[BugRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM bug_records ORDER BY collected_at DESC"
            ).fetchall()
        return [BugRecord.from_dict(json.loads(r[0])) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM bug_records").fetchone()[0]

    def delete(self, bug_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM bug_records WHERE id = ?", (bug_id,))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db)
        conn.isolation_level = None  # autocommit
        return conn


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Cannot serialize {type(obj)}")
