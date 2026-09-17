import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from test_accounting import observation, task
from server.codex_stats_server.accounting import ledger, payload
from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.reports import telegram_week
from server.codex_stats_server.telegram_bot import TelegramBot
from server.codex_stats_server.http_api import create_server


class AdjustmentTests(unittest.TestCase):
    def test_idle_usage_in_total_without_tasks(self):
        book = ledger([], [observation(100, 20), observation(200, 24)])
        result = payload([], book['entries'])
        self.assertEqual(result['observed_weekly_percent'], 4)
        self.assertEqual(result['unallocated_percent'], 4)

    def test_bonus_scales_both_computers_and_untracked(self):
        tasks = [task('a', 'GamePC', 100, 160), task('b', 'Lenovo', 160, 176)]
        events = [observation(100, 0), observation(160, 60), observation(176, 76),
                  observation(180, 80), observation(190, 60), observation(200, 60)]
        book = ledger(tasks, events)
        result = payload(tasks, book['entries'])
        self.assertEqual(len(book['windows']), 1)
        self.assertEqual({r['machine_name']: r['weekly_percent'] for r in result['rows']},
                         {'GamePC': 45, 'Lenovo': 12})
        self.assertEqual(result['unallocated_percent'], 3)
        self.assertEqual(result['observed_weekly_percent'], 60)
        self.assertEqual(book['adjustments'][0]['factor'], .75)
        self.assertEqual(book, ledger(tasks, list(reversed(events)) * 2))

    def test_late_start_does_not_invent_unobserved_usage(self):
        events = [observation(100, 60), observation(150, 80), observation(180, 60), observation(200, 61)]
        book = ledger([task('a', 'A')], events)
        self.assertEqual(sum(e['percent'] for e in book['entries']), 16)
        self.assertEqual(len(book['windows']), 1)

    def test_repeated_bonuses_rounding_conservation_and_cap(self):
        tasks = [task(str(i), str(i), 100, 500) for i in range(3)]
        events = [observation(100, 0), observation(150, 99), observation(180, 70), observation(200, 70),
                  observation(250, 80), observation(280, 60), observation(300, 60), observation(400, 100)]
        book = ledger(tasks, events)
        self.assertEqual(len(book['adjustments']), 2)
        self.assertEqual(round(sum(e['percent'] for e in book['entries']), 2), 100)
        for entry in book['entries']:
            self.assertAlmostEqual(entry['percent'], sum(entry['machines'].values()) + entry['unallocated'])
            self.assertAlmostEqual(sum(entry['tasks'].values()), sum(entry['machines'].values()))
        self.assertAlmostEqual(sum(r['weekly_percent'] for r in payload(tasks, book['entries'])['rows']), 100)

    def test_zero_reset_and_single_stale_reading_not_bonus(self):
        book = ledger([], [observation(100, 80), observation(150, 60), observation(160, 81)])
        self.assertEqual(book['adjustments'], [])
        book = ledger([], [observation(100, 80), observation(150, 0), observation(160, 1)])
        self.assertEqual(book['adjustments'], [])
        self.assertEqual(len(book['windows']), 2)

    def test_further_drop_requires_another_confirmation(self):
        events = [observation(100, 80), observation(150, 60), observation(160, 50)]
        self.assertFalse(ledger([], events)['adjustments'])
        self.assertEqual(ledger([], events + [observation(170, 50)])['adjustments'][0]['after_used'], 50)


class BonusNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = StatsDatabase(Path(self.temp.name) / 'db.sqlite')
        self.bot = TelegramBot('test', {1}, {2, 3}, self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def apply_bonus(self):
        for at, used in ((100, 0), (150, 80), (180, 60), (200, 60)):
            self.db.apply_event(observation(at, used))
            self.bot.notify_quota_adjustments()

    def test_broadcast_persists_retries_only_failed_and_ignores_filter(self):
        self.db.set_setting('reports:2', '{"mode":"mine","machines":[]}')
        self.db.disable_bot_user(3)
        self.apply_bonus()
        calls = []
        def request(method, data):
            calls.append(data['chat_id'])
            if data['chat_id'] == 2:
                raise RuntimeError('offline')
        self.bot._request = request
        self.bot._flush_quota_notices()
        self.assertEqual(calls, [1, 2])
        fresh = TelegramBot('test', {1}, set(), self.db)
        fresh._request = lambda method, data: calls.append(data['chat_id'])
        fresh.notify_quota_adjustments()
        fresh._flush_quota_notices()
        fresh._flush_quota_notices()
        self.assertEqual(calls, [1, 2, 2])
        notice = next(iter(json.loads(self.db.get_setting('quota_notices')).values()))
        self.assertIn('20% → 40%', notice['text'])
        self.assertIn('пересчитаны пропорционально', notice['text'])
        self.assertIn('Траты без системы учета: 60%', telegram_week(self.db))

    def test_initial_historical_replay_does_not_spam(self):
        self.apply_bonus()
        with self.db._lock, self.db._connection:
            self.db._connection.execute("DELETE FROM settings WHERE key IN ('quota_notices','quota_notices_initialized')")
        fresh = TelegramBot('test', {1}, set(), self.db)
        calls = []
        fresh._request = lambda *args: calls.append(args)
        fresh._flush_quota_notices()
        self.assertEqual(calls, [])

    def test_zero_category_always_shown(self):
        self.db.apply_event(observation(100, 10))
        self.assertIn('• Траты без системы учета: 0%\nВсего: 0%', telegram_week(self.db))

    def test_http_idle_snapshots_notify_without_task_completion(self):
        server = create_server('127.0.0.1', 0, self.db, 'test-key',
                               event_notifier=self.bot.notify_quota_adjustments)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for at, used in ((100, 80), (150, 60), (200, 60), (200, 60)):
                request = urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}/api/v1/events',
                    data=json.dumps(observation(at, used)).encode(),
                    headers={'Authorization': 'Bearer test-key', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 202)
            notices = json.loads(self.db.get_setting('quota_notices'))
            self.assertEqual(len(notices), 1)
            self.assertEqual(sorted(next(iter(notices.values()))['pending']), [1, 2, 3])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
