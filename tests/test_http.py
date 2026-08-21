from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.http_api import create_server


class HttpApiTests(unittest.TestCase):
    def test_health_auth_event_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = StatsDatabase(Path(directory) / "http.sqlite")
            server = create_server("127.0.0.1", 0, database, "test-key")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as response:
                    self.assertEqual(json.load(response)["status"], "ok")

                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(base + "/api/v1/stats", timeout=2)
                self.assertEqual(context.exception.code, 401)
                context.exception.close()

                now = time.time()
                payload = {
                    "event_id": "task:start",
                    "event_type": "task_started",
                    "account_fingerprint": "account",
                    "user_name": "Иван",
                    "machine_id": "pc-1",
                    "machine_name": "PC-1",
                    "task_id": "task",
                    "started_at": now,
                    "start_quota": {"used_percent": 5, "resets_at": 999, "source": "test"},
                    "quota": {"used_percent": 5, "resets_at": 999, "source": "test"},
                    "tokens": {},
                }
                request = urllib.request.Request(
                    base + "/api/v1/events",
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertTrue(json.load(response)["accepted"])

                request = urllib.request.Request(
                    base + "/api/v1/stats",
                    headers={"Authorization": "Bearer test-key"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(json.load(response)["active_tasks"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                database.close()


if __name__ == "__main__":
    unittest.main()
