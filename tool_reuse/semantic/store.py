from __future__ import annotations

import json
import sqlite3
from array import array
from pathlib import Path
from typing import Any

from ..jsonutil import stable_json
from .models import SemanticCall, SemanticEntry


SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_entries (
  cache_scope TEXT NOT NULL,
  record_key TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding_provider TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL,
  embedding_blob BLOB NOT NULL,
  source_path TEXT NOT NULL,
  semantic_version TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  operation_kind TEXT NOT NULL,
  semantic_text TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  freshness_class TEXT NOT NULL,
  ttl_seconds INTEGER NOT NULL,
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
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (cache_scope, record_key, embedding_provider, embedding_model)
);

CREATE INDEX IF NOT EXISTS idx_semantic_entries_lookup
ON semantic_entries(
  cache_scope, embedding_provider, embedding_model, operation_kind, success,
  expires_at_epoch
);
"""


class SemanticStore:
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
            self._drop_unscoped_legacy_index()
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self.db_path.chmod(0o600)
        else:
            try:
                self._require_scoped_schema()
            except ValueError:
                self.conn.close()
                raise

    def _drop_unscoped_legacy_index(self) -> None:
        columns = self._table_columns()
        if columns and "cache_scope" not in columns:
            self.conn.execute("DROP TABLE semantic_entries")
            self.conn.commit()
            self.conn.execute("VACUUM")

    def _require_scoped_schema(self) -> None:
        columns = self._table_columns()
        if columns and "cache_scope" not in columns:
            raise ValueError(
                "semantic-v2 database has no cache scope; rebuild it with semantic-v3"
            )

    def _table_columns(self) -> set[str]:
        return {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(semantic_entries)")
        }

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def delete_source_index(
        self,
        source_path: str,
        cache_scope: str,
        embedding_provider: str,
        embedding_model: str,
    ) -> None:
        """Remove one source/config slice before rebuilding it."""
        self.conn.execute(
            """
            DELETE FROM semantic_entries
            WHERE source_path = ? AND cache_scope = ?
              AND embedding_provider = ? AND embedding_model = ?
            """,
            (source_path, cache_scope, embedding_provider, embedding_model),
        )

    def upsert(self, entry: SemanticEntry) -> None:
        call = entry.call
        self.conn.execute(
            """
            INSERT INTO semantic_entries (
              cache_scope, record_key, embedding_model, embedding_provider,
              embedding_dim,
              embedding_blob, source_path, semantic_version, tool_name,
              action_kind, operation_kind, semantic_text, metadata_json,
              freshness_class, ttl_seconds, started_at, ended_at,
              observed_at_epoch, expires_at_epoch, success, status_reason,
              tool_input_json, tool_response_json, response_text, response_sha256
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(cache_scope, record_key, embedding_provider, embedding_model)
            DO UPDATE SET
              embedding_dim=excluded.embedding_dim,
              embedding_blob=excluded.embedding_blob,
              source_path=excluded.source_path,
              semantic_version=excluded.semantic_version,
              tool_name=excluded.tool_name,
              action_kind=excluded.action_kind,
              operation_kind=excluded.operation_kind,
              semantic_text=excluded.semantic_text,
              metadata_json=excluded.metadata_json,
              freshness_class=excluded.freshness_class,
              ttl_seconds=excluded.ttl_seconds,
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
              imported_at=CURRENT_TIMESTAMP
            """,
            (
                entry.cache_scope,
                entry.record_key,
                entry.embedding_model,
                entry.embedding_provider,
                len(entry.embedding),
                _vector_to_blob(entry.embedding),
                entry.source_path,
                call.semantic_version,
                call.tool_name,
                call.action_kind,
                call.operation_kind,
                call.semantic_text,
                stable_json(call.metadata),
                call.freshness_class,
                call.ttl_seconds,
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
            ),
        )

    def candidates(
        self,
        cache_scope: str,
        embedding_provider: str,
        embedding_model: str,
        operation_kind: str,
        semantic_version: str,
        *,
        successful_only: bool = True,
        limit: int = 50,
    ) -> list[SemanticEntry]:
        if limit <= 0:
            return []
        sql = """
            SELECT * FROM semantic_entries
            WHERE cache_scope = ? AND embedding_provider = ? AND embedding_model = ?
              AND operation_kind = ? AND semantic_version = ?
        """
        parameters: list[Any] = [
            cache_scope,
            embedding_provider,
            embedding_model,
            operation_kind,
            semantic_version,
        ]
        if successful_only:
            sql += " AND success = 1"
        sql += " ORDER BY ended_at DESC LIMIT ?"
        parameters.append(limit)
        rows = self.conn.execute(sql, parameters).fetchall()
        return [_row_to_entry(row) for row in rows]

    def stats(self, cache_scope: str) -> dict[str, Any]:
        total = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM semantic_entries WHERE cache_scope = ?",
                (cache_scope,),
            ).fetchone()[0]
        )
        rows = self.conn.execute(
            """
            SELECT embedding_provider, embedding_model, operation_kind,
                   COUNT(*) AS count,
                   SUM(success) AS success_count
            FROM semantic_entries
            WHERE cache_scope = ?
            GROUP BY embedding_provider, embedding_model, operation_kind
            ORDER BY embedding_provider, embedding_model, operation_kind
            """,
            (cache_scope,),
        ).fetchall()
        return {
            "cache_scope": cache_scope,
            "total": total,
            "indexes": [
                {
                    "embedding_provider": row["embedding_provider"],
                    "embedding_model": row["embedding_model"],
                    "operation_kind": row["operation_kind"],
                    "count": int(row["count"]),
                    "success": int(row["success_count"] or 0),
                }
                for row in rows
            ],
        }


def _vector_to_blob(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _blob_to_vector(blob: bytes, dimension: int) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    if len(values) != dimension:
        raise ValueError(f"Corrupt embedding: expected {dimension}, got {len(values)}")
    return list(values)


def _row_to_entry(row: sqlite3.Row) -> SemanticEntry:
    call = SemanticCall(
        semantic_version=row["semantic_version"],
        tool_name=row["tool_name"],
        action_kind=row["action_kind"],
        operation_kind=row["operation_kind"],
        semantic_text=row["semantic_text"],
        metadata=json.loads(row["metadata_json"]),
        freshness_class=row["freshness_class"],
        ttl_seconds=int(row["ttl_seconds"]),
    )
    return SemanticEntry(
        cache_scope=row["cache_scope"],
        record_key=row["record_key"],
        source_path=row["source_path"],
        call=call,
        embedding_provider=row["embedding_provider"],
        embedding_model=row["embedding_model"],
        embedding=_blob_to_vector(row["embedding_blob"], int(row["embedding_dim"])),
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
    )
