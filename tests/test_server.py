from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from server.codex_stats_server.allocation import allocate_weekly_percent
from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.reports import stats_payload


def event(
    event_type: str,
    task_id: str,
    *,
    user: str,
    machine: str,
    started: float,
    finished: float | None = None,
    used: float = 10,
    tokens: int = 0,
) -> dict:
    return {
        "event_id": f"{task_id}:{event_type}",
        "event_type": event_type,
        "account_fingerprint": "account-1",
        "user_name": user,
        "machine_id": machine,
        "machine_name": machine,
        "task_id": task_id,
        "turn_id": task_id,
        "session_id": "session",
        "started_at": started,
        "finished_at": finished,
        "start_quota": {"used_percent": 10, "resets_at": 999, "source": "test"},
        "quota": {"used_percent": used, "resets_at": 999, "source": "test"},
        "tokens": {"total_tokens": tokens},
    }


class AllocationTests(unittest.TestCase):
    def test_overlapping_tasks_share_one_global_delta(self) -> None:
        tasks = [
            {
                "task_id": "a",
                "account_fingerprint": "account",
                "status": "completed",
                "started_at": 0,
                "finished_at": 10,
                "start_used_percent": 20,
                "end_used_percent": 24,
                "start_reset_at": 100,
                "end_reset_at": 100,
                "total_tokens": 100,
            },
            {
                "task_id": "b",
                "account_fingerprint": "account",
                "status": "completed",
                "started_at": 5,
                "finished_at": 15,
                "start_used_percent": 21,
                "end_used_percent": 26,
                "start_reset_at": 100,
                "end_reset_at": 100,
                "total_tokens": 200,
            },
        ]
        allocations = allocate_weekly_percent(tasks)
        self.assertAlmostEqual(sum(item.weekly_percent or 0 for item in allocations.values()), 6)
        self.assertAlmostEqual(allocations["a"].weekly_percent or 0, 2)
        self.assertAlmostEqual(allocations["b"].weekly_percent or 0, 4)
        self.assertEqual(allocations["a"].confidence, "allocated_overlap")


class DatabaseTests(unittest.TestCase):
    def test_idempotent_events_and_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = StatsDatabase(Path(directory) / "test.sqlite")
            try:
                now = time.time()
                start = event("task_started", "task-1", user="Иван", machine="PC-1", started=now - 100)
                finish = event(
                    "task_completed",
                    "task-1",
                    user="Иван",
                    machine="PC-1",
                    started=now - 100,
                    finished=now,
                    used=13,
                    tokens=123_456,
                )
                self.assertTrue(database.apply_event(start))
                self.assertFalse(database.apply_event(start))
                self.assertTrue(database.apply_event(finish))
                payload = stats_payload(database, days=365)
                self.assertEqual(len(payload["rows"]), 1)
                self.assertEqual(payload["rows"][0]["total_tokens"], 123_456)
                self.assertEqual(payload["rows"][0]["weekly_percent"], 3)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
