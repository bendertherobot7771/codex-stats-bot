from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.codex_stats_agent.models import QuotaSnapshot
from agent.codex_stats_agent.quota import QuotaError, apply_reset_credits, quota_from_usage_response, quota_from_log_payload
from server.codex_stats_server.accounting import ledger, payload
from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.reports import quota_suffix, task_completed_message, weekly_windows, stats_payload


def quota(at, used, reset=999999):
    return dict(captured_at=at, used_percent=used, resets_at=reset, window_minutes=10080,
                source="oauth_usage_endpoint", reset_credits_count=0)


def task(tid, machine, start=100, end=200):
    return dict(task_id=tid, machine_id=machine, machine_name=machine, user_name=machine,
                account_fingerprint="a", started_at=start, finished_at=end, status="completed")


def observation(at, used, reset=999999):
    return dict(event_id=f"sample:{at}", event_type="quota_snapshot", task_id="quota_snapshot",
                account_fingerprint="a", quota=quota(at, used, reset))


class AccountingTests(unittest.TestCase):
    def test_three_pcs_conserve_100_including_display_rounding(self):
        tasks = [task(str(i), f"PC{i}") for i in range(3)]
        events = [observation(100, 0), observation(200, 100)]
        book = ledger(tasks, events)
        values = book["entries"][0]["machines"]
        self.assertEqual(sorted(values.values()), [33.33, 33.33, 33.34])
        self.assertAlmostEqual(sum(values.values()), 100)

    def test_more_tasks_on_same_pc_do_not_increase_pc_weight(self):
        tasks = [task("a1", "A"), task("a2", "A"), task("b", "B")]
        book = ledger(tasks, [observation(100, 20), observation(200, 30)])
        self.assertEqual(book["entries"][0]["machines"], {"A": 5, "B": 5})

    def test_duration_weighting(self):
        tasks = [task("a", "A"), task("b", "B", 150, 200)]
        book = ledger(tasks, [observation(100, 0), observation(200, 9)])
        self.assertEqual(book["entries"][0]["machines"], {"A": 6, "B": 3})

    def test_out_of_order_and_duplicates_are_identical(self):
        tasks = [task("a", "A"), task("b", "B", 130, 170)]
        events = [observation(100, 10), observation(130, 12), observation(170, 18), observation(200, 20)]
        expected = ledger(tasks, events)
        shuffled = events * 2
        random.Random(7).shuffle(shuffled)
        self.assertEqual(expected, ledger(tasks, shuffled))

    def test_automatic_reset_same_reset_time_zeros_all_pcs(self):
        tasks = [task("a", "A"), task("b", "B")]
        book = ledger(tasks, [observation(100, 0), observation(140, 80), observation(150, 0), observation(151, 0)])
        current = [e for e in book["entries"] if e["epoch"] == book["current"]["a"]]
        self.assertEqual(payload(tasks, current)["observed_weekly_percent"], 0)
        self.assertEqual(len(book["windows"]), 2)
        self.assertEqual(sum(e["percent"] for e in book["entries"]), 80)

    def test_usage_after_reset_is_separate_and_never_above_100(self):
        tasks = [task("a", "A"), task("b", "B")]
        events = [observation(100, 0), observation(140, 100), observation(150, 0), observation(151, 0), observation(200, 100)]
        book = ledger(tasks, events)
        self.assertEqual(sum(e["percent"] for e in book["entries"]), 200)
        for epoch in book["windows"]:
            self.assertLessEqual(sum(e["percent"] for e in book["entries"] if e["epoch"] == epoch), 100)
        current = [e for e in book["entries"] if e["epoch"] == book["current"]["a"]]
        self.assertEqual(payload(tasks, current)["observed_weekly_percent"], 100)

    def test_scheduled_reset_changes_window_even_when_percent_increases(self):
        book = ledger([task("a", "A")], [observation(100, 10, 150), observation(150, 20, 999)])
        self.assertEqual(len(book["windows"]), 2)
        self.assertFalse(book["entries"])

    def test_gap_is_unallocated_not_charged_to_next_pc(self):
        book = ledger([task("a", "A", 150, 200)], [observation(100, 10), observation(200, 20)])
        self.assertEqual(book["entries"][0]["unallocated"], 10)
        self.assertFalse(book["entries"][0]["machines"])

    def test_crashed_task_does_not_take_future_pc_usage(self):
        stale = task("stale", "OldPC", 1, None)
        stale["status"] = "active"
        current = task("live", "NewPC", 200, 300)
        book = ledger([stale, current], [observation(200, 10), observation(300, 20)])
        self.assertEqual(book["entries"][0]["machines"], {"NewPC": 10})

    def test_stale_start_and_non_weekly_snapshot_not_used(self):
        event = observation(100, 10)
        event.update(event_type="task_started", started_at=1)
        book = ledger([], [event])
        self.assertFalse(book["windows"])
        event = observation(100, 10)
        event["quota"]["window_minutes"] = 300
        self.assertFalse(ledger([], [event])["windows"])

    def test_log_decrease_cannot_reset_direct_counter(self):
        stale = observation(150, 0)
        stale["quota"]["source"] = "codex_session_log"
        book = ledger([task("a", "A")], [observation(100, 20), stale, observation(200, 30)])
        self.assertEqual(len(book["windows"]), 1)
        self.assertEqual(book["entries"][0]["percent"], 10)

    def test_random_parallel_intervals_never_overcharge(self):
        rng = random.Random(19)
        for _ in range(100):
            tasks = [task(str(i), str(i % 3), 100 + rng.randrange(40), 160 + rng.randrange(41)) for i in range(8)]
            events = [observation(100 + i, i) for i in range(101)]
            book = ledger(tasks, events)
            total = sum(sum(e["machines"].values()) + e["unallocated"] for e in book["entries"])
            self.assertAlmostEqual(total, 100)
            for entry in book["entries"]:
                self.assertAlmostEqual(sum(entry["tasks"].values()), sum(entry["machines"].values()))


class MessageTests(unittest.TestCase):
    def test_exact_compact_message_and_reset_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StatsDatabase(Path(directory) / "db.sqlite")
            try:
                base = task("t", "GamePC", 100, 200)
                for kind, at, used in [("task_started", 100, 20), ("task_completed", 200, 24)]:
                    q = quota(at, used, 200 + 74 * 3600)
                    q.update(reset_credits_count=1, reset_credits_expire_at=[200 + 338 * 3600])
                    event = dict(base, event_id=kind, event_type=kind, start_quota=quota(100, 20, q["resets_at"]), quota=q)
                    db.apply_event(event)
                with patch("server.codex_stats_server.reports.time.time", return_value=200):
                    text = task_completed_message(db.completed_tasks()[0], db)
                self.assertEqual(text, "GamePC · 4% за неделю · До сброса 3д 2ч · Сброс 1 на 14д 2ч\n"
                                      "До задания: 80% >>> После задания: 76%\n"
                                      "Изменение за время задания: 4% Учтено за GamePC: 4%")
                db.apply_event(observation(201, 0, q["resets_at"]))
                db.apply_event(observation(202, 0, q["resets_at"]))
                self.assertEqual(stats_payload(db)["rows"][0]["weekly_percent"], 0)
                self.assertEqual(len(weekly_windows(db)), 2)
                self.assertEqual(weekly_windows(db)[1]["observed_weekly_percent"], 4)
            finally:
                db.close()

    def test_credits_hidden_when_zero_or_expired(self):
        self.assertEqual(quota_suffix(dict(resets_at=3600, reset_credits_count=0), now=0), "До сброса 0д 1ч")
        self.assertNotIn(" · Сброс", quota_suffix(dict(resets_at=3600, reset_credits_count=1, reset_credits_expire_at=[1]), now=2))

    def test_unknown_credit_expiry_is_not_invented(self):
        self.assertIn("Сброс 1 · срок неизвестен", quota_suffix(dict(reset_credits_count=1)))


class QuotaSafetyTests(unittest.TestCase):
    def test_one_lagging_api_response_does_not_reset_counters(self):
        book = ledger([task("a", "A")], [observation(100, 20), observation(130, 30), observation(150, 29), observation(200, 32)])
        self.assertEqual(len(book["windows"]), 1)
        self.assertEqual(sum(e["percent"] for e in book["entries"]), 12)

    def test_five_hour_window_is_not_weekly(self):
        with self.assertRaises(QuotaError):
            quota_from_usage_response({"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 20}}})
        self.assertIsNone(quota_from_log_payload({"primary": {"window_minutes": 300}}))
        self.assertIsNone(quota_from_log_payload({"limit_id": "other-model", "primary": {"window_minutes": 10080}}))

    def test_credit_metadata_is_sanitized(self):
        snapshot = QuotaSnapshot()
        apply_reset_credits(snapshot, {"credits": [
            {"status": "available", "reset_type": "codex_rate_limits", "expires_at": "2026-10-05T04:19:54Z", "id": "private-id"},
            {"status": "redeemed", "reset_type": "codex_rate_limits"}]})
        self.assertEqual(snapshot.reset_credits_count, 1)
        self.assertEqual(len(snapshot.reset_credits_expire_at), 1)
        self.assertNotIn("private-id", str(snapshot.to_dict()))
