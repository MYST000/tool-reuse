from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..jsonutil import stable_json
from .models import ExactCall, ExactEntry


SCHEMA = """
CREATE TABLE IF NOT EXISTS exact_entries (
  record_key TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  key_version TEXT NOT NULL,
  exact_key TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  operation_kind TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  freshness_class TEXT NOT NULL,
  ttl_seconds INTEGER NOT NULL,
  replayable INTEGER NOT NULL,
  replay_policy TEXT NOT NULL,
  policy_reason TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  observed_at_epoch INTEGER,
  expires_at_epoch INTEGER,
  success INTEGER NOT NULL,
  status_reason TEXT NOT NULL,
  tool_input_json TEXT NOT NULL,
  tool_response_json TEXT NOT NULL,
  response_text TEXT NOT NULL,
  response_sha256 TEXT NOT NULL,
  source_record_json TEXT NOT NULL,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_exact_entries_key
ON exact_entries(exact_key, success, ended_at DESC);

CREATE INDEX IF NOT EXISTS idx_exact_entries_operation
ON exact_entries(operation_kind, success, replayable, expires_at_epoch);
"""


class ExactStore:
    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        if read_only:
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self.db_path.chmod(0o600)

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def delete_source(self, source_path: str) -> None:
        """Remove entries from a source before rebuilding that source's index."""
        self.conn.execute(
            "DELETE FROM exact_entries WHERE source_path = ?", (source_path,)
        )

    def upsert(self, entry: ExactEntry) -> None:
        call = entry.exact_call
        self.conn.execute(
            """
            INSERT INTO exact_entries (
              record_key, source_path, key_version, exact_key, tool_name,
              action_kind, operation_kind, canonical_json, freshness_class,
              ttl_seconds, replayable, replay_policy, policy_reason, started_at,
              ended_at, observed_at_epoch, expires_at_epoch, success,
              status_reason, tool_input_json, tool_response_json, response_text,
              response_sha256, source_record_json
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(record_key) DO UPDATE SET
              source_path=excluded.source_path,
              key_version=excluded.key_version,
              exact_key=excluded.exact_key,
              tool_name=excluded.tool_name,
              action_kind=excluded.action_kind,
              operation_kind=excluded.operation_kind,
              canonical_json=excluded.canonical_json,
              freshness_class=excluded.freshness_class,
              ttl_seconds=excluded.ttl_seconds,
              replayable=excluded.replayable,
              replay_policy=excluded.replay_policy,
              policy_reason=excluded.policy_reason,
              started_at=excluded.started_at,
              ended_at=excluded.ended_at,
              observed_at_epoch=excluded.observed_at_epoch,
              expires_at_epoch=excluded.expires_at_epoch,
              success=excluded.success,
              status_reason=excluded.status_reason,
              tool_input_json=excluded.tool_input_json,
              tool_response_json=excluded.tool_response_json,
              response_text=excluded.response_text,
              response_sha256=excluded.response_sha256,
              source_record_json=excluded.source_record_json,
              imported_at=CURRENT_TIMESTAMP
            """,
            (
                entry.record_key,
                entry.source_path,
                call.key_version,
                call.exact_key,
                call.tool_name,
                call.action_kind,
                call.operation_kind,
                stable_json(call.canonical),
                call.freshness_class,
                call.ttl_seconds,
                int(call.replayable),
                call.replay_policy,
                call.reason,
                entry.started_at,
                entry.ended_at,
                entry.observed_at_epoch,
                entry.expires_at_epoch,
                int(entry.success),
                entry.status_reason,
                stable_json(entry.tool_input),
                stable_json(entry.tool_response),
                entry.response_text,
                entry.response_sha256,
                stable_json(entry.source_record),
            ),
        )

    def find(self, exact_key: str, limit: int = 20) -> list[ExactEntry]:
        rows = self.conn.execute(
            """
            SELECT * FROM exact_entries
            WHERE exact_key = ?
            ORDER BY success DESC, ended_at DESC
            LIMIT ?
            """,
            (exact_key, limit),
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        total = int(
            self.conn.execute("SELECT COUNT(*) FROM exact_entries").fetchone()[0]
        )
        rows = self.conn.execute(
            """
            SELECT operation_kind, COUNT(*) AS count,
                   SUM(success) AS success_count,
                   SUM(replayable) AS replayable_count
            FROM exact_entries
            GROUP BY operation_kind
            ORDER BY operation_kind
            """
        ).fetchall()
        return {
            "total": total,
            "operations": {
                row["operation_kind"]: {
                    "count": int(row["count"]),
                    "success": int(row["success_count"] or 0),
                    "replayable": int(row["replayable_count"] or 0),
                }
                for row in rows
            },
        }


def _row_to_entry(row: sqlite3.Row) -> ExactEntry:
    call = ExactCall(
        exact_key=row["exact_key"],
        key_version=row["key_version"],
        tool_name=row["tool_name"],
        action_kind=row["action_kind"],
        operation_kind=row["operation_kind"],
        canonical=json.loads(row["canonical_json"]),
        freshness_class=row["freshness_class"],
        ttl_seconds=int(row["ttl_seconds"]),
        replayable=bool(row["replayable"]),
        replay_policy=row["replay_policy"],
        reason=row["policy_reason"],
    )
    return ExactEntry(
        record_key=row["record_key"],
        source_path=row["source_path"],
        exact_call=call,
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        observed_at_epoch=row["observed_at_epoch"],
        expires_at_epoch=row["expires_at_epoch"],
        success=bool(row["success"]),
        status_reason=row["status_reason"],
        tool_input=json.loads(row["tool_input_json"]),
        tool_response=json.loads(row["tool_response_json"]),
        response_text=row["response_text"],
        response_sha256=row["response_sha256"],
        source_record=json.loads(row["source_record_json"]),
    )
