from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bridge.api import APIError
from bridge.service import deliver_pending, send_command_replies
from bridge.state import Store
from test_bridge import CFG, StoreFixture, photo_update, private_update, update


class QueueTests(StoreFixture):
    def run_queue(self, tg, vk, now):
        with patch("bridge.state.time.time", return_value=now):
            deliver_pending(self.store, CFG, tg, vk)

    def test_album_debounce_blocks_later_text_and_keeps_original_position(self):
        self.store.ingest([photo_update(1, "a", "#важно Альбом"),
                           update(2, text="#важно После альбома")], CFG, now=101)
        order = self.store.queue_head()["queue_order"]
        self.assertEqual(self.store.due(now=102), [])
        self.store.ingest([photo_update(3, "a")], CFG, now=104)
        self.assertEqual(self.store.queue_head()["queue_order"], order)
        self.assertEqual(self.store.due(now=108), [])
        tg, vk = Mock(), Mock()
        tg.photo.return_value = b"jpeg"
        vk.upload_photo.side_effect = ["photo-1_1", "photo-1_2"]
        self.run_queue(tg, vk, 109)
        sent = vk.send.call_args_list
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].args[2], ["photo-1_1", "photo-1_2"])
        self.assertIn("После альбома", sent[1].args[1])
        self.assertIsNone(self.store.queue_head())

    def test_unsorted_updates_and_equal_deadlines_follow_update_order(self):
        self.store.ingest([update(3), update(1), update(2)], CFG, now=101)
        self.assertEqual([j["id"] for j in self.store.due(now=101)],
                         ["-10042:msg:1", "-10042:msg:2", "-10042:msg:3"])
        self.assertEqual(self.store.offset, 4)

    def test_download_retry_blocks_same_batch_and_survives_restart(self):
        self.store.ingest([photo_update(1, caption="#важно Фото"), update(2)], CFG, now=101)
        tg, vk = Mock(), Mock()
        tg.photo.side_effect = APIError("Telegram", "NETWORK", True, 60)
        with self.assertLogs(level="WARNING"):
            self.run_queue(tg, vk, 102)
        vk.send.assert_not_called()
        self.store.close()
        self.store = Store(self.directory)
        tg.photo.side_effect = None
        tg.photo.return_value = b"jpeg"
        vk.upload_photo.return_value = "photo-1_1"
        self.run_queue(tg, vk, 161)
        vk.send.assert_not_called()
        self.run_queue(tg, vk, 162)
        self.assertEqual(len(vk.send.call_args_list), 2)
        self.assertEqual(vk.send.call_args_list[0].args[2], ["photo-1_1"])
        self.assertEqual(vk.send.call_args_list[1].args[2], [])

    def test_partial_message_and_rate_limit_finish_before_next_job(self):
        self.store.ingest([update(1, text="#важно " + "x" * 7000),
                           update(2, text="#важно Следующее")], CFG, now=101)
        tg, vk = Mock(), Mock()
        vk.send.side_effect = [1, APIError("VK", "HTTP_429", True, 60)]
        with self.assertLogs(level="WARNING"):
            self.run_queue(tg, vk, 102)
        failed_call = vk.send.call_args
        head = self.store.queue_head()
        plan = json.loads(head["plan"])
        self.assertEqual(head["cursor"], 1)
        self.assertEqual(vk.send.call_count, 2)
        self.store.close()
        self.store = Store(self.directory)
        vk.send.side_effect = None
        vk.send.reset_mock()
        self.run_queue(tg, vk, 161)
        vk.send.assert_not_called()
        self.run_queue(tg, vk, 162)
        sent = vk.send.call_args_list
        self.assertEqual(sent[0], failed_call)
        self.assertEqual([c.args[3] for c in sent[:-1]], [p["random_id"] for p in plan[1:]])
        self.assertIn("Следующее", sent[-1].args[1])

    def test_permanent_error_blocks_batch_but_status_and_retry_work(self):
        self.store.ingest([update(1), update(2)], CFG, now=101)
        tg, vk = Mock(), Mock()
        vk.send.side_effect = APIError("VK", 15)
        with self.assertLogs(level="WARNING"):
            self.run_queue(tg, vk, 102)
        self.assertEqual(vk.send.call_count, 1)
        failed_call = vk.send.call_args
        order = self.store.queue_head()["queue_order"]
        self.assertEqual(self.store.due(now=999), [])
        replies = self.store.ingest([private_update(3, "/status")], CFG, now=103)
        self.assertIn("Очередь остановлена", replies[0]["text"])
        self.assertIn("/skip -10042:msg:1", replies[0]["text"])
        send_command_replies(self.store, CFG, tg)
        tg.call.assert_called_once()
        self.store.ingest([private_update(4, "/retry@VKNioForwarderbot")], CFG, now=104)
        self.assertEqual(self.store.queue_head()["queue_order"], order)
        vk.send.side_effect = None
        vk.send.reset_mock()
        self.run_queue(tg, vk, 104)
        self.assertEqual(vk.send.call_args_list[0], failed_call)
        self.assertEqual(vk.send.call_count, 2)
        self.assertIsNone(self.store.queue_head())

    def fail_head(self):
        self.store.ingest([update(1), update(2)], CFG, now=101)
        head = self.store.queue_head()
        self.store.save_plan(head["id"], [
            {"text": "Already sent", "attachments": []},
            {"text": "Not sent", "attachments": []}])
        self.store.advance(head["id"], 1)
        self.store.upload(head["id"], "file", "doc-1_2")
        self.store.failure(head, APIError("VK", 15), now=102)
        return head["id"]

    def test_skip_requires_exact_failed_head_and_does_not_repeat_sent_parts(self):
        job_id = self.fail_head()
        self.store.ingest([private_update(3, "/skip"),
                           private_update(4, "/skip -10042:msg:2"),
                           private_update(5, "/retry extra")], CFG, now=103)
        self.assertEqual(self.store.queue_head()["status"], "failed")
        replies = self.store.ingest([private_update(6, f"/skip {job_id}")], CFG, now=104)
        self.assertIn("Неотправленная часть", replies[0]["text"])
        skipped = self.store.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["body"], "[]")
        self.assertIsNone(skipped["plan"])
        self.assertIsNone(self.store.upload(job_id, "file"))
        tg, vk = Mock(), Mock()
        self.run_queue(tg, vk, 105)
        vk.send.assert_called_once()
        self.assertIn("Объявление", vk.send.call_args.args[1])

    def test_foreign_private_group_and_spoofed_commands_cannot_change_queue(self):
        job_id = self.fail_head()
        replies = self.store.ingest([
            private_update(3, "/retry", sender=88),
            update(4, text=f"/skip {job_id}"),
            private_update(5, f"/skip {job_id}", **{"from": {"id": 88}}),
        ], CFG, now=103)
        self.assertEqual(replies, [])
        self.assertEqual(self.store.queue_head()["id"], job_id)
        self.assertEqual(self.store.queue_head()["status"], "failed")

    def test_skip_reply_checkpoint_and_cache_cleanup_are_atomic(self):
        job_id = self.fail_head()
        command = private_update(3, f"/skip {job_id}")
        malformed = update(4)
        del malformed["message"]["message_id"]
        with self.assertRaises(KeyError):
            self.store.ingest([command, malformed], CFG, now=103)
        self.assertEqual(self.store.offset, 3)
        self.assertEqual(self.store.queue_head()["status"], "failed")
        self.assertEqual(self.store.upload(job_id, "file"), "doc-1_2")
        self.assertEqual(self.store.due_replies(now=999), [])
        self.store.ingest([command], CFG, now=104)
        second = self.store.queue_head()
        self.store.failure(second, APIError("VK", 15), now=105)
        self.assertEqual(self.store.ingest([command], CFG, now=106), [])
        self.assertEqual(self.store.queue_head()["status"], "failed")
        self.assertEqual(len(self.store.due_replies(now=999)), 1)

    def test_retry_command_checkpoint_does_not_retry_twice(self):
        self.fail_head()
        command = private_update(3, "/retry")
        self.store.ingest([command], CFG, now=103)
        self.store.failure(self.store.queue_head(), APIError("VK", 15), now=104)
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.ingest([command], CFG, now=105), [])
        self.assertEqual(self.store.queue_head()["status"], "failed")

    def test_temporary_failure_cannot_be_skipped_and_status_explains_wait(self):
        self.store.ingest([update(1), update(2)], CFG, now=101)
        self.store.failure(self.store.queue_head(), APIError("Telegram", "NETWORK", True, 60), now=102)
        replies = self.store.ingest([private_update(3, "/skip -10042:msg:1"),
                                    private_update(4, "/status")], CFG, now=103)
        self.assertIn("автоматически", replies[0]["text"])
        self.assertIn("Ожидает повтора", replies[1]["text"])
        self.assertEqual(self.store.due(now=104), [])

    def test_cli_retry_preserves_position_relative_to_existing_pending_job(self):
        self.fail_head()
        self.assertEqual(self.store.retry_failed(), 1)
        self.assertEqual([j["id"] for j in self.store.due(now=104)],
                         ["-10042:msg:1", "-10042:msg:2"])

    def test_stop_mid_delivery_keeps_next_job_waiting(self):
        self.store.ingest([photo_update(1, caption="#важно Фото"), update(2)], CFG, now=101)
        tg, vk, stop = Mock(), Mock(), threading.Event()
        def download(_):
            stop.set()
            return b"jpeg"
        tg.photo.side_effect = download
        vk.upload_photo.return_value = "photo-1_1"
        deliver_pending(self.store, CFG, tg, vk, stop)
        vk.send.assert_not_called()
        self.assertEqual(self.store.queue_head()["id"], "-10042:msg:1")


class QueueMigrationTests(unittest.TestCase):
    def test_legacy_queue_migration_preserves_insertion_order_and_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite3"
            # The actual schema before queue_order, including an unfinished plan.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, selected INTEGER NOT NULL,
                    due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    plan TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT)""")
                db.execute("CREATE TABLE uploads (job_id TEXT, file_id TEXT, attachment TEXT, PRIMARY KEY(job_id,file_id))")
                db.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", [
                    ("z-first", '[{"text":"old"}]', 1, 150, "pending", '[{"random_id":123}]', 1, 2, "NETWORK"),
                    ("a-second", "[]", 1, 101, "pending", None, 0, 0, None),
                    ("done", "[]", 1, 100, "done", None, 1, 0, None),
                ])
                db.execute("INSERT INTO uploads VALUES('z-first','file','doc-1_2')")
                before = db.execute("SELECT * FROM jobs ORDER BY rowid").fetchall()
            for _ in range(2):
                store = Store(directory)
                try:
                    after = store.db.execute("SELECT * FROM jobs ORDER BY queue_order").fetchall()
                    self.assertEqual([tuple(row)[:-1] for row in after], before)
                    self.assertEqual([row["queue_order"] for row in after], [1, 2, 3])
                    self.assertEqual(store.upload("z-first", "file"), "doc-1_2")
                    self.assertEqual(store.due(now=149), [])
                    self.assertEqual([row["id"] for row in store.due(now=150)], ["z-first", "a-second"])
                finally:
                    store.close()
            # An older binary can still insert rows after rollback; reopening fills them in.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO jobs(id,body,selected,due) VALUES('after-rollback','[]',1,1)")
            store = Store(directory)
            try:
                rows = store.db.execute("SELECT queue_order FROM jobs ORDER BY queue_order").fetchall()
                self.assertEqual([row[0] for row in rows], [1, 2, 3, 4])
                self.assertEqual([row["id"] for row in store.due(now=150)],
                                 ["z-first", "a-second", "after-rollback"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
