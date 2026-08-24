# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Offline queue — SQLite-based queue for metrics when Mothership is unreachable."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class OfflineQueue:
    """SQLite-based offline queue for metrics snapshots."""

    def __init__(self, db_path: str, max_days: int = 14):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_days = max_days
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    retries INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivered ON queue(delivered_at)")

    def add(self, payload: dict[str, Any]) -> None:
        """Add a metrics payload to the queue."""
        record = {
            "payload": json.dumps(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO queue (payload, created_at, retries) VALUES (?, ?, 0)",
                (record["payload"], record["created_at"]),
            )

    def drain(self, max_items: int = 100) -> list[dict[str, Any]]:
        """Return undelivered item payloads, ordered by age."""
        return [payload for _, payload in self.drain_with_ids(max_items)]

    def drain_with_ids(self, max_items: int = 100) -> list[tuple[int, dict[str, Any]]]:
        """Return undelivered queue item IDs and payloads, ordered by age."""
        self.cleanup_old()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, payload FROM queue WHERE delivered_at IS NULL ORDER BY id LIMIT ?",
                (max_items,),
            ).fetchall()
        return [(int(row[0]), json.loads(row[1])) for row in rows]

    def mark_delivered(self, item_ids: list[int]) -> None:
        """Mark queue items as delivered."""
        if not item_ids:
            return
        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" * len(item_ids))
            conn.execute(
                f"UPDATE queue SET delivered_at = ? WHERE id IN ({placeholders})",
                [datetime.now(timezone.utc).isoformat()] + item_ids,
            )

    def cleanup_old(self) -> int:
        """Remove items older than max_days. Returns count removed."""
        cutoff = datetime.now(timezone.utc).timestamp() - self.max_days * 86400
        cutoff_str = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "DELETE FROM queue WHERE created_at < ?",
                (cutoff_str,),
            ).rowcount
        return count

    def size(self) -> int:
        """Return count of undelivered items."""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM queue WHERE delivered_at IS NULL"
            ).fetchone()[0]
