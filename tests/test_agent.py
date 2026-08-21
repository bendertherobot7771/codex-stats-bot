from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent.codex_stats_agent.config import AgentConfig
from agent.codex_stats_agent.__main__ import main
from agent.codex_stats_agent.models import QuotaSnapshot
from agent.codex_stats_agent.quota import quota_from_log_payload, quota_from_usage_response
from agent.codex_stats_agent.watcher import CodexWatcher


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, event: dict) -> None:
        self.events.append(event)


class QuotaParsingTests(unittest.TestCase):
    def test_selects_weekly_window(self) -> None:
        snapshot = quota_from_usage_response(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 12,
                        "limit_window_seconds": 18_000,
                        "reset_at": 100,
                    },
                    "secondary_window": {
                        "used_percent": 34.5,
                        "limit_window_seconds": 604_800,
                        "reset_at": 200,
                    },
                }
            }
        )
        self.assertEqual(snapshot.used_percent, 34.5)
        self.assertEqual(snapshot.window_minutes, 10_080)
        self.assertEqual(snapshot.resets_at, 200)

    def test_parses_desktop_log_window(self) -> None:
        snapshot = quota_from_log_payload(
            {
                "limit_id": "codex",
                "primary": {"used_percent": 7, "window_minutes": 10_080, "resets_at": 123},
            }
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.used_percent, 7)
        self.assertEqual(snapshot.source, "codex_session_log")


class WatcherTests(unittest.TestCase):
    def test_emits_start_heartbeat_and_finish_without_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            (codex_home / "sessions").mkdir(parents=True)
            (codex_home / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "account-1", "access_token": "unused"}}),
                encoding="utf-8",
            )
            config = AgentConfig(
                server_url="http://localhost:1",
                api_key="secret",
                user_name="Иван",
                machine_id="machine-1",
                machine_name="PC-1",
                codex_home=str(codex_home),
                heartbeat_interval_seconds=1,
            )
            client = FakeClient()
            watcher = CodexWatcher(
                config,
                state_path=root / "state.json",
                queue_path=root / "queue.json",
                client=client,
            )
            watcher._fresh_quota = lambda fallback=None: QuotaSnapshot(  # type: ignore[method-assign]
                used_percent=11,
                window_minutes=10_080,
                resets_at=999,
                source="test",
            )

            file_key = str(codex_home / "sessions" / "task.jsonl")
            watcher._handle_item(
                file_key,
                {
                    "type": "session_meta",
                    "payload": {"session_id": "session-1", "id": "thread-1", "cwd": "C:/Project"},
                },
            )
            watcher._handle_item(
                file_key,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-21T10:00:00Z",
                    "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 1000},
                },
            )
            watcher._handle_item(
                file_key,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 50,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 120,
                            }
                        },
                        "rate_limits": {
                            "primary": {"used_percent": 12, "window_minutes": 10_080, "resets_at": 999}
                        },
                    },
                },
            )
            watcher._handle_item(
                file_key,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-21T10:01:00Z",
                    "payload": {"type": "task_complete", "turn_id": "turn-1", "completed_at": 1060},
                },
            )

            self.assertEqual([event["event_type"] for event in client.events], [
                "task_started",
                "task_heartbeat",
                "task_completed",
            ])
            finish = client.events[-1]
            self.assertEqual(finish["tokens"]["total_tokens"], 120)
            serialized = json.dumps(finish, ensure_ascii=False)
            self.assertNotIn("секретный запрос", serialized)
            self.assertNotIn("access_token", serialized)


class ConfigurationTests(unittest.TestCase):
    def test_reconfigure_preserves_machine_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with redirect_stdout(io.StringIO()):
                first = main(
                    [
                        "--config",
                        str(path),
                        "configure",
                        "--server-url",
                        "http://old",
                        "--api-key",
                        "old-key",
                        "--user-name",
                        "Иван",
                    ]
                )
            original = AgentConfig.load(path)
            with redirect_stdout(io.StringIO()):
                second = main(
                    [
                        "--config",
                        str(path),
                        "configure",
                        "--server-url",
                        "http://new",
                        "--api-key",
                        "new-key",
                        "--user-name",
                        "Пётр",
                    ]
                )
            updated = AgentConfig.load(path)
            self.assertEqual((first, second), (0, 0))
            self.assertEqual(updated.machine_id, original.machine_id)
            self.assertEqual(updated.machine_name, original.machine_name)
            self.assertEqual(updated.server_url, "http://new")
            self.assertEqual(updated.user_name, "Пётр")


if __name__ == "__main__":
    unittest.main()
