import json
import tempfile
import unittest
from pathlib import Path

from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.telegram_bot import TelegramBot, _telegram_commands


class ReportPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = StatsDatabase(Path(self.temp.name) / 'stats.sqlite')
        self.bot = TelegramBot('test', {1}, {2, 3}, self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def recipients(self, machine):
        self.bot.notify_completion('report', machine)
        result = []
        while not self.bot.outgoing.empty():
            result.append(self.bot.outgoing.get_nowait()[1]['chat_id'])
        return sorted(result)

    def callback(self, chat, data):
        self.bot._handle_callback({'id': 'test', 'data': data,
            'message': {'message_id': 1, 'chat': {'id': chat}}})
        while not self.bot.outgoing.empty():
            self.bot.outgoing.get_nowait()

    def test_all_members_default_without_start_registration(self):
        self.assertEqual(self.recipients('GamePC'), [1, 2, 3])
        self.db.disable_bot_user(3)
        self.assertEqual(self.recipients('Lenovo'), [1, 2])

    def test_mine_filter_empty_selection_and_switch_back(self):
        self.callback(2, 'reports:mine')
        self.assertEqual(self.recipients('GamePC'), [1, 3])
        self.db.set_setting('reports:2', json.dumps({'mode': 'mine', 'machines': ['Lenovo', 'Other']}))
        self.assertEqual(self.recipients('Lenovo'), [1, 2, 3])
        self.assertEqual(self.recipients('Other'), [1, 2, 3])
        self.assertEqual(self.recipients('GamePC'), [1, 3])
        self.callback(2, 'reports:all')
        self.assertEqual(self.recipients('GamePC'), [1, 2, 3])
        self.assertEqual(self.bot.report_preferences(2)['machines'], ['Lenovo', 'Other'])

    def test_selection_persists_and_is_personal(self):
        class Devices:
            def devices(self):
                return [{'machine_id': 'lenovo-id', 'machine_name': 'Lenovo'}]
        self.bot.lifecycle = Devices()
        _, markup = self.bot._reports_message(2)
        data = markup['inline_keyboard'][1][0]['callback_data']
        self.assertLessEqual(len(data.encode()), 64)
        self.callback(2, data)
        self.assertEqual(self.bot.report_preferences(2)['machines'], ['lenovo-id'])
        fresh = TelegramBot('test', {1}, set(), self.db)
        self.assertEqual(fresh.report_preferences(2)['machines'], ['lenovo-id'])
        self.assertEqual(fresh.report_preferences(1)['machines'], [])
        self.callback(2, data)
        self.assertEqual(self.bot.report_preferences(2)['machines'], [])

    def test_unknown_user_cannot_set_preferences_and_menu_present(self):
        self.callback(99, 'reports:mine')
        self.assertEqual(self.db.get_setting('reports:99'), '')
        self.assertIn('reports', [c['command'] for c in _telegram_commands()])

    def test_maintenance_is_not_filtered(self):
        self.db.register_chat(1)
        self.db.register_chat(2)
        self.callback(2, 'reports:mine')
        calls = []
        self.bot._request = lambda method, data: calls.append(data['chat_id'])
        self.bot.announce_maintenance('maintenance')
        self.assertEqual(sorted(calls), [1, 2])

    def test_server_updated_broadcast_ignores_personal_filter(self):
        self.callback(2, 'reports:mine')
        self.db.disable_bot_user(3)
        self.bot.notify_all('Серверная часть обновлена. Ждите обновления клиентов.')
        chats = []
        while not self.bot.outgoing.empty():
            chats.append(self.bot.outgoing.get_nowait()[1]['chat_id'])
        self.assertEqual(sorted(chats), [1, 2])
