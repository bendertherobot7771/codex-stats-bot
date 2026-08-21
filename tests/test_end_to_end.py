from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.codex_stats_agent.api import ServerClient
from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.http_api import create_server
from server.codex_stats_server.reports import stats_payload


class EndToEndTests(unittest.TestCase):
    def test_two_agents_send_overlapping_tasks_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = StatsDatabase(Path(directory) / "e2e.sqlite")
            server = create_server("127.0.0.1", 0, database, "shared-key")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = ServerClient(f"http://127.0.0.1:{server.server_address[1]}", "shared-key")
            now = time.time()

            def send(event_type: str, task: str, machine: str, start: float, end: float | None, used: float, tokens: int) -> None:
                client.send(
                    {
                        "event_id": f"{task}:{event_type}",
                        "event_type": event_type,
                        "account_fingerprint": "same-account",
                        "user_name": machine,
                        "machine_id": machine,
                        "machine_name": machine,
                        "task_id": task,
                        "started_at": start,
                        "finished_at": end,
                        "start_quota": {"used_percent": 40, "resets_at": 999, "source": "test"},
                        "quota": {"used_percent": used, "resets_at": 999, "source": "test"},
                        "tokens": {"total_tokens": tokens},
                    }
                )

            try:
                send("task_started", "task-a", "PC-A", now, None, 40, 0)
                send("task_started", "task-b", "PC-B", now + 1, None, 41, 0)
                send("task_completed", "task-a", "PC-A", now, now + 10, 44, 100)
                send("task_completed", "task-b", "PC-B", now + 1, now + 12, 46, 200)

                payload = stats_payload(database, days=7)
                self.assertEqual(len(payload["rows"]), 2)
                self.assertAlmostEqual(payload["observed_weekly_percent"], 6)
                by_machine = {row["machine_name"]: row for row in payload["rows"]}
                self.assertAlmostEqual(by_machine["PC-A"]["weekly_percent"], 2)
                self.assertAlmostEqual(by_machine["PC-B"]["weekly_percent"], 4)
                self.assertEqual(by_machine["PC-A"]["estimated_tasks"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                database.close()


if __name__ == "__main__":
    unittest.main()

