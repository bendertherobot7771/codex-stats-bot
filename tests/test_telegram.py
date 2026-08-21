from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.telegram_bot import TelegramBot


class TelegramAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = StatsDatabase(Path(self.temporary.name) / "bot.sqlite")
        self.bot = TelegramBot("test-token", {8461749755}, set(), self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def command(self, chat_id: int, text: str) -> tuple[str, dict]:
        self.bot._handle_update({"message": {"chat": {"id": chat_id}, "text": text}})
        return self.bot.outgoing.get_nowait()

    def test_admin_can_add_and_remove_viewer(self) -> None:
        method, payload = self.command(8461749755, "/adduser 123 Иван")
        self.assertEqual(method, "sendMessage")
        self.assertIn("добавлен", payload["text"])
        self.assertEqual(self.database.bot_user(123)["role"], "viewer")

        _, payload = self.command(123, "/stats")
        self.assertNotIn("Доступ запрещён", payload["text"])
        _, payload = self.command(8461749755, "/removeuser 123")
        self.assertIn("удалён", payload["text"])
        _, payload = self.command(123, "/stats")
        self.assertIn("Доступ запрещён", payload["text"])

    def test_viewer_cannot_manage_users_and_admin_cannot_be_removed(self) -> None:
        self.database.ensure_bot_user(123, "viewer")
        _, payload = self.command(123, "/adduser 456")
        self.assertNotIn("добавлен", payload["text"])
        self.assertIsNone(self.database.bot_user(456))
        _, payload = self.command(8461749755, "/removeuser 8461749755")
        self.assertIn("администратором", payload["text"])


if __name__ == "__main__":
    unittest.main()
