# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Sentinel offline queue behavior."""

from __future__ import annotations

from sentinel.queue import OfflineQueue


def test_drain_returns_payloads_without_marking_delivered(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.add({"sequence": 1})
    queue.add({"sequence": 2})

    assert queue.drain() == [{"sequence": 1}, {"sequence": 2}]
    assert queue.size() == 2


def test_drain_with_ids_allows_marking_delivered_items(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    queue.add({"sequence": 1})
    queue.add({"sequence": 2})

    items = queue.drain_with_ids()
    assert [payload for _, payload in items] == [{"sequence": 1}, {"sequence": 2}]

    queue.mark_delivered([items[0][0]])

    assert queue.size() == 1
    assert queue.drain() == [{"sequence": 2}]


def test_drain_respects_max_items(tmp_path):
    queue = OfflineQueue(str(tmp_path / "queue.db"))
    for sequence in range(3):
        queue.add({"sequence": sequence})

    items = queue.drain_with_ids(max_items=2)

    assert len(items) == 2
    assert [payload for _, payload in items] == [{"sequence": 0}, {"sequence": 1}]
