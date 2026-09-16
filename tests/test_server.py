from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.reports import stats_payload, telegram_weeks, weekly_windows


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
        "start_quota": {"used_percent": 10, "resets_at": 999, "source": "oauth_usage_endpoint", "window_minutes": 10080, "captured_at": started},
        "quota": {"used_percent": used, "resets_at": 999, "source": "oauth_usage_endpoint", "window_minutes": 10080, "captured_at": finished if finished is not None else started},
        "tokens": {"total_tokens": tokens},
    }


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

    def test_bot_users_and_weekly_history_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = StatsDatabase(Path(directory) / "test.sqlite")
            try:
                database.ensure_bot_user(8461749755, "admin", "Владелец")
                database.ensure_bot_user(123, "viewer", "Иван", 8461749755)
                self.assertEqual(database.bot_user(8461749755)["role"], "admin")
                self.assertEqual(database.bot_user(123)["display_name"], "Иван")
                self.assertTrue(database.disable_bot_user(123))
                self.assertIsNone(database.bot_user(123))
                self.assertFalse(database.disable_bot_user(8461749755))

                now = time.time()
                for task_id, reset in (("old", 1_000), ("new", 2_000)):
                    now += 200
                    start = event("task_started", task_id, user="Иван", machine="PC-1", started=now - 100)
                    finish = event("task_completed", task_id, user="Иван", machine="PC-1", started=now - 100, finished=now, used=12, tokens=100)
                    start["start_quota"]["resets_at"] = reset
                    start["quota"]["resets_at"] = reset
                    finish["start_quota"]["resets_at"] = reset
                    finish["quota"]["resets_at"] = reset
                    database.apply_event(start)
                    database.apply_event(finish)
                windows = weekly_windows(database)
                self.assertEqual([item["reset_at"] for item in windows], [2_000, 1_000])
                self.assertEqual(windows[0]["rows"][0]["machine_name"], "PC-1")
                text, markup = telegram_weeks(database, page=1, page_size=1)
                self.assertIn("страница 1/2", text)
                self.assertEqual(markup["inline_keyboard"][-1][0]["callback_data"], "weeks:2")
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
