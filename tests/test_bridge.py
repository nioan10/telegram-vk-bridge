from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from bridge.api import APIError, MAX_PHOTO_BYTES, Telegram, Transport, VK
from bridge.config import Config, ConfigError, read_env
from bridge.content import extract, render, split_text
from bridge.service import check, deliver, send_command_replies
from bridge.state import Store, process_lock, read_status


CFG = Config("123:test", "vk-test-secret", -10042, 2000000007, telegram_owner_id=77)


def message(number=1, text="#важно Объявление", **extra):
    data = {"message_id": number, "date": 101,
            "chat": {"id": CFG.telegram_chat_id, "type": "supergroup"},
            "from": {"first_name": "Автор"}, "text": text}
    if "#" in text:
        start = text.index("#")
        tag = text[start:].split()[0].rstrip("!,.")
        data["entities"] = [{"type": "hashtag", "offset": len(text[:start].encode('utf-16-le')) // 2,
                              "length": len(tag.encode('utf-16-le')) // 2}]
    data.update(extra)
    return data


def update(number=1, **extra):
    return {"update_id": number, "message": message(number, **extra)}


def photo_update(number, album=None, caption=""):
    msg = message(number, text=caption)
    msg["caption"] = msg.pop("text")
    msg["caption_entities"] = msg.pop("entities", [])
    msg["photo"] = [{"file_id": f"file-{number}", "width": 100, "height": 100, "file_size": 100}]
    if album:
        msg["media_group_id"] = album
    return {"update_id": number, "message": msg}


def private_update(number=1, text="Личное объявление", sender=77, **extra):
    return update(number, text=text, chat={"id": sender, "type": "private"},
                  **dict({"from": {"id": sender, "first_name": "Владелец"}}, **extra))


class ContentTests(unittest.TestCase):
    def test_all_mode_config_keeps_tags_and_ignores_service_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("HASHTAG=OFF\nSTRIP_HASHTAG=true\n", encoding="utf-8")
            cfg = Config.load(path, "status")
        self.assertEqual(cfg.hashtag, "off")
        item = extract(message(text="#важно Остаётся в тексте"), cfg)
        self.assertTrue(item["selected"])
        self.assertIn("#важно", item["text"])
        self.assertFalse(extract(message(text="", pinned_message={"message_id": 9}), cfg)["selected"])

    def test_forwarded_author_is_preserved(self):
        msg = message(forward_origin={"type": "hidden_user", "sender_user_name": "Иван"})
        self.assertEqual(extract(msg, CFG)["author"], "Переслано от: Иван")

    def test_full_hashtag_and_case(self):
        self.assertTrue(extract(message(text="#ВАЖНО! Встреча"), CFG)["selected"])
        self.assertFalse(extract(message(text="#важное Встреча"), CFG)["selected"])
        self.assertFalse(extract(message(text="важно Встреча"), CFG)["selected"])
        # A URL fragment or a code block isn't a Telegram hashtag entity.
        self.assertFalse(extract(message(text="https://site.test/#важно", entities=[]), CFG)["selected"])

    def test_utf16_emoji_offsets_strip_and_hidden_link(self):
        msg = message(text="😀 #важно Ссылка")
        msg["entities"].append({"type": "text_link", "offset": 10, "length": 6, "url": "https://example.org"})
        item = extract(msg, replace(CFG, strip_hashtag=True))
        self.assertTrue(item["selected"])
        self.assertEqual(item["text"], "😀  Ссылка (https://example.org)")

    def test_caption_and_largest_photo(self):
        msg = photo_update(1, caption="#важно Фото")["message"]
        msg["photo"].append({"file_id": "large", "width": 1000, "height": 1000})
        item = extract(msg, CFG)
        self.assertTrue(item["selected"])
        self.assertEqual(item["photo"]["file_id"], "large")

    def test_unsupported_attachment_is_explicit(self):
        item = extract(message(video={"file_id": "video"}), CFG)
        text, photos = render([item], CFG)
        self.assertIn("Не перенесены вложения: видео", text)
        self.assertEqual(photos, [])

    def test_utf16_chunking_roundtrip(self):
        text = "a" * 3499 + "😀" * 4000 + "конец"
        chunks = split_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 3500 for c in chunks))

    def test_tag_only_does_not_produce_empty_send(self):
        cfg = replace(CFG, strip_hashtag=True, include_author=False)
        text, photos = render([extract(message(text="#важно"), cfg)], cfg)
        self.assertTrue(text)
        self.assertEqual(photos, [])


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.store.start(CFG, 123, now=100)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class StateTests(StoreFixture):
    def test_tag_off_forwards_content_and_preserves_scope_and_restart(self):
        replies = self.store.ingest([private_update(1, "/tag OFF"),
            update(2, text="Без тега"), photo_update(3),
            update(4, text="Чужой чат", chat={"id": -999, "type": "supergroup"}),
            update(5, text="Старое", date=99),
            update(6, text="", new_chat_title="Новая тема"),
            update(7, text="Закрыто", has_protected_content=True),
            private_update(8, "Чужая личка", sender=88)], CFG, now=101)
        self.assertIn("все новые сообщения", replies[0]["text"])
        self.assertEqual([j["id"] for j in self.store.due(now=102)], ["-10042:msg:2", "-10042:msg:3"])
        self.store.close()
        self.store = Store(self.directory)
        self.store.start(CFG, 123)
        self.assertEqual(self.store.get("hashtag"), "off")
        self.store.ingest([update(9, text="После перезапуска")], CFG, now=102)
        self.assertEqual(len(self.store.due(now=103)), 3)

    def test_tag_off_can_restore_filter_in_same_batch(self):
        self.store.ingest([private_update(1, "/tag off"), update(2, text="Проходит"),
            private_update(3, "/tag #важно"), update(4, text="Не проходит"), update(5)], CFG, now=101)
        self.assertEqual([j["id"] for j in self.store.due(now=102)], ["-10042:msg:2", "-10042:msg:5"])
        self.assertEqual(self.store.get("hashtag"), "#важно")

    def test_tag_off_album_status_and_owner_only(self):
        self.assertEqual(self.store.ingest([private_update(1, "/tag off", sender=88)], CFG, now=101), [])
        self.assertEqual(self.store.get("hashtag"), "#важно")
        self.store.ingest([private_update(2, "/tag off"), photo_update(3, "album"), photo_update(4, "album")], CFG, now=101)
        job = self.store.due(now=110)[0]
        self.assertEqual(job["selected"], 1)
        self.assertEqual(len(json.loads(job["body"])), 2)
        replies = self.store.ingest([private_update(5, "/tag"), private_update(6, "/status")], CFG, now=102)
        self.assertTrue(all("без фильтра по хештегу" in r["text"] for r in replies))

    def test_only_owner_can_submit_private_messages(self):
        events = [private_update(1), private_update(2, sender=88),
                  private_update(3, **{"from": {"id": 88}})]
        self.store.ingest(events, CFG, now=101)
        jobs = self.store.due(now=102)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "77:msg:1")

    def test_private_mode_can_be_disabled(self):
        self.store.ingest([private_update()], replace(CFG, private_inbox_enabled=False), now=101)
        self.assertFalse(self.store.due(now=102))

    def test_tag_command_is_persistent_and_ordered_within_batch(self):
        events = [update(1), private_update(2, "/tag #срочно"),
                  update(3), update(4, text="#срочно Новость"), private_update(5, "Без тега")]
        replies = self.store.ingest(events, CFG, now=101)
        self.assertEqual(len(replies), 1)
        self.assertIn("Сохранено", replies[0]["text"])
        self.assertEqual([job["id"] for job in self.store.due(now=102)],
                         ["-10042:msg:1", "-10042:msg:4", "77:msg:5"])
        self.store.close()
        self.store = Store(self.directory)
        self.store.start(CFG, 123)
        self.assertEqual(self.store.get("hashtag"), "#срочно")

    def test_unauthorized_command_never_changes_filter_or_gets_reply(self):
        replies = self.store.ingest([private_update(1, "/tag #другое", sender=88)], CFG, now=101)
        self.assertEqual(replies, [])
        self.assertEqual(self.store.get("hashtag"), "#важно")
        self.assertFalse(self.store.due(now=102))

    def test_invalid_tag_preserves_setting(self):
        for number, command in enumerate(("/tag on", "/tag #два #тега", "/tag #"), 1):
            replies = self.store.ingest([private_update(number, command)], CFG, now=101)
            self.assertIn("не изменена", replies[0]["text"])
            self.assertEqual(self.store.get("hashtag"), "#важно")

    def test_command_checkpoint_prevents_old_command_overwriting_new_tag(self):
        first = private_update(1, "/tag #первое")
        self.store.ingest([first, private_update(2, "/tag #второе")], CFG, now=101)
        self.assertEqual(self.store.ingest([first], CFG, now=102), [])
        self.assertEqual(self.store.get("hashtag"), "#второе")

    def test_commands_never_go_to_vk(self):
        replies = self.store.ingest([private_update(1, "/help"), private_update(2, "/tag"),
                                     private_update(3, "/status"), private_update(4, "/bridge_id")], CFG, now=101)
        self.assertEqual(len(replies), 4)
        self.assertIn("#важно", replies[1]["text"])
        self.assertIn("В очереди: 0", replies[2]["text"])
        self.assertIn("77", replies[3]["text"])
        self.assertFalse(self.store.due(now=102))

    def test_only_selected_source_new_messages(self):
        foreign = update(2, chat={"id": -999, "type": "supergroup"})
        edited = {"update_id": 4, "edited_message": message(4)}
        self.store.ingest([update(1, text="Без тега"), foreign,
                           update(3, date=99), edited,
                           update(5, has_protected_content=True), update(6)], CFG, now=101)
        self.assertEqual(self.store.offset, 7)
        jobs = self.store.due(now=102)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["id"].endswith("msg:6"))

    def test_checkpoint_and_job_are_atomic_on_error(self):
        malformed = update(2)
        del malformed["message"]["message_id"]
        with self.assertRaises(KeyError):
            self.store.ingest([update(1), malformed], CFG, now=101)
        self.assertEqual(self.store.offset, 0)
        self.assertEqual(len(self.store.due(now=102)), 0)

    def test_album_tag_on_second_part_and_debounce(self):
        self.store.ingest([photo_update(1, "a")], CFG, now=101)
        self.store.ingest([photo_update(2, "a", "#важно Альбом")], CFG, now=104)
        self.assertEqual(len(self.store.due(now=108)), 0)
        job = self.store.due(now=109)[0]
        self.assertEqual(job["selected"], 1)
        self.assertEqual(len(json.loads(job["body"])), 2)

    def test_untagged_album_scrubbed_after_wait(self):
        self.store.ingest([photo_update(1, "a")], CFG, now=101)
        deliver(self.store.due(now=110)[0], self.store, CFG, Mock(), Mock())
        row = self.store.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["status"], "ignored")
        self.assertEqual(row["body"], "[]")

    def test_duplicates_and_restart(self):
        event = update()
        self.store.ingest([event, event], CFG, now=101)
        job = self.store.due(now=102)[0]
        self.store.finish(job["id"])
        self.store.close()
        self.store = Store(self.directory)
        self.store.start(CFG, 123, now=999)
        self.store.ingest([event], CFG, now=1000)
        self.assertEqual(self.store.offset, 2)
        self.assertEqual(self.store.get("started_at"), "100")
        self.assertEqual(len(self.store.due(now=1000)), 0)

    def test_route_or_token_cannot_change_under_queued_data(self):
        for cfg in (replace(CFG, vk_peer_id=2000000008), replace(CFG, vk_token="other"),
                    replace(CFG, telegram_chat_id=-10044)):
            with self.assertRaises(ConfigError):
                self.store.start(cfg, 123)

    def test_transient_backoff_and_permanent_recovery(self):
        self.store.ingest([update()], CFG, now=101)
        job = self.store.due(now=101)[0]
        self.store.failure(job, APIError("VK", "HTTP_429", True, 60), now=101)
        self.assertFalse(self.store.due(now=160))
        job = self.store.due(now=161)[0]
        self.store.failure(job, APIError("VK", 15), now=161)
        self.assertFalse(self.store.due(now=999))
        self.assertEqual(self.store.retry_failed(), 1)
        self.assertEqual(len(self.store.due(now=999)), 1)

    def test_lock_excludes_another_instance(self):
        with process_lock(self.directory):
            with self.assertRaises(ConfigError):
                with process_lock(self.directory):
                    self.fail("Second instance acquired lock")
        with process_lock(self.directory):
            pass

    def test_readonly_status(self):
        self.store.ingest([update()], CFG, now=101)
        self.store.heartbeat()
        counts, meta, failures = read_status(self.directory)
        self.assertEqual(counts["pending"], 1)
        self.assertIn("heartbeat", meta)
        self.assertEqual(failures, [])


class DeliveryTests(StoreFixture):
    def test_command_reply_survives_network_error_and_restart(self):
        command = private_update(1, "/tag off")
        self.store.ingest([command], CFG, now=101)
        tg = Mock(); tg.call.side_effect = APIError("Telegram", "NETWORK", True, 60)
        with patch("bridge.state.time.time", return_value=102), self.assertLogs(level="WARNING"):
            send_command_replies(self.store, CFG, tg)
        self.assertFalse(self.store.due_replies(now=161))
        self.store.close(); self.store = Store(self.directory)
        self.store.ingest([command], CFG, now=163)
        self.assertEqual(self.store.get("hashtag"), "off")
        self.assertEqual(len(self.store.due_replies(now=163)), 1)
        tg.call.side_effect = None
        with patch("bridge.state.time.time", return_value=163):
            send_command_replies(self.store, CFG, tg)
        self.assertEqual(tg.call.call_args.kwargs["chat_id"], CFG.telegram_owner_id)
        self.assertIn("без фильтра", tg.call.call_args.kwargs["text"])
        self.assertFalse(self.store.due_replies(now=999))
        self.assertEqual(self.store.get("last_reply"), "163")
        self.assertFalse(self.store.due(now=999))

    def test_rejected_reply_does_not_block_later_commands(self):
        self.store.ingest([private_update(1, "/status"), private_update(2, "/help")], CFG, now=101)
        tg = Mock(); tg.call.side_effect = [APIError("Telegram", 403), True]
        with self.assertLogs(level="WARNING"):
            send_command_replies(self.store, CFG, tg)
        self.assertEqual(tg.call.call_count, 2)
        self.assertFalse(self.store.due_replies(now=999))

    def test_private_ack_failure_does_not_repeat_vk_delivery(self):
        self.store.ingest([private_update()], CFG, now=101)
        tg, vk = Mock(), Mock()
        tg.call.side_effect = APIError("Telegram", "NETWORK", True)
        with self.assertLogs(level="WARNING"):
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        self.assertEqual(vk.send.call_count, 1)
        self.assertFalse(self.store.due(now=102))
        self.assertEqual(tg.call.call_args.kwargs["chat_id"], 77)

    def test_private_forwarded_album_needs_no_hashtag(self):
        events = [photo_update(1, "album"), photo_update(2, "album", "Подпись")]
        for event in events:
            event["message"]["chat"] = {"id": 77, "type": "private"}
            event["message"]["from"] = {"id": 77, "first_name": "Владелец"}
        self.store.ingest(events, CFG, now=101)
        tg, vk = Mock(), Mock()
        tg.photo.return_value = b"jpeg"
        vk.upload_photo.side_effect = ["photo-1_1", "photo-1_2"]
        deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        self.assertEqual(vk.send.call_args.args[2], ["photo-1_1", "photo-1_2"])
        self.assertIn("Отправлено в VK", tg.call.call_args.kwargs["text"])

    def test_retry_uses_same_payload_and_id_after_uncertain_send(self):
        self.store.ingest([update()], CFG, now=101)
        vk, tg = Mock(), Mock()
        vk.send.side_effect = APIError("VK", "NETWORK", True)
        job = self.store.due(now=102)[0]
        with self.assertRaises(APIError):
            deliver(job, self.store, CFG, tg, vk)
        first = vk.send.call_args
        self.store.close()
        self.store = Store(self.directory)
        vk.send.side_effect = None
        deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        self.assertEqual(first, vk.send.call_args)
        self.assertGreater(first.args[3], 0)
        self.assertEqual(len(self.store.due(now=102)), 0)
        self.assertEqual(self.store.db.execute("SELECT body FROM jobs").fetchone()[0], "[]")

    def test_long_message_resume_skips_confirmed_parts(self):
        self.store.ingest([update(text="#важно " + "😀" * 3500)], CFG, now=101)
        vk = Mock()
        vk.send.side_effect = [1, APIError("VK", "NETWORK", True)]
        with self.assertRaises(APIError):
            deliver(self.store.due(now=102)[0], self.store, CFG, Mock(), vk)
        failed_call = vk.send.call_args
        self.assertEqual(self.store.due(now=102)[0]["cursor"], 1)
        vk.send.reset_mock(side_effect=True)
        deliver(self.store.due(now=102)[0], self.store, CFG, Mock(), vk)
        self.assertEqual(vk.send.call_args_list[0], failed_call)
        self.assertEqual(vk.send.call_count, 2)

    def test_album_upload_and_send(self):
        self.store.ingest([photo_update(1, "a", "#важно Фото"), photo_update(2, "a")], CFG, now=101)
        tg, vk = Mock(), Mock()
        tg.photo.return_value = b"jpeg"
        vk.upload_photo.side_effect = ["photo-1_1", "photo-1_2"]
        deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        self.assertEqual(tg.photo.call_count, 2)
        self.assertEqual(vk.send.call_count, 1)
        self.assertEqual(vk.send.call_args.args[2], ["photo-1_1", "photo-1_2"])

    def test_upload_resume_reuses_completed_photos(self):
        self.store.ingest([photo_update(1, "a", "#важно Фото"), photo_update(2, "a")], CFG, now=101)
        tg, vk = Mock(), Mock()
        tg.photo.return_value = b"jpeg"
        vk.upload_photo.side_effect = ["photo-1_1", APIError("VK", "NETWORK", True)]
        with self.assertRaises(APIError):
            deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        vk.upload_photo.side_effect = ["photo-1_2"]
        deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        self.assertEqual(tg.photo.call_count, 3)
        self.assertEqual(vk.send.call_args.args[2], ["photo-1_1", "photo-1_2"])

    def test_oversize_does_not_silently_drop_attachment(self):
        event = photo_update(1, caption="#важно Фото")
        event["message"]["photo"][0]["file_size"] = MAX_PHOTO_BYTES + 1
        self.store.ingest([event], CFG, now=101)
        vk = Mock()
        with self.assertRaises(APIError):
            deliver(self.store.due(now=102)[0], self.store, CFG, Mock(), vk)
        vk.send.assert_not_called()


class APITests(unittest.TestCase):
    def test_telegram_getupdates_shape(self):
        transport = Mock()
        transport.form.return_value = {"ok": True, "result": []}
        Telegram("123:token", transport).updates(42)
        params = transport.form.call_args.args[2]
        self.assertEqual(params["offset"], 42)
        self.assertEqual(json.loads(params["allowed_updates"]), ["message"])
        self.assertEqual(transport.form.call_args.kwargs["timeout"], 10)

    def test_api_errors_never_include_token_or_server_description(self):
        transport = Mock()
        transport.form.return_value = {"error": {"error_code": 15, "error_msg": "SECRET", "request_params": ["SECRET"]}}
        with self.assertRaises(APIError) as error:
            VK("SECRET", transport=transport).call("messages.send")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertFalse(error.exception.retryable)

    def test_transport_hides_urls_and_parses_retry_after(self):
        transport = Transport()
        transport.opener = Mock()
        transport.opener.open.side_effect = URLError("https://host/token=SECRET")
        with self.assertRaises(APIError) as result:
            transport.request("https://example.org", "VK")
        self.assertEqual(str(result.exception), "VK: NETWORK")
        transport.opener.open.side_effect = HTTPError("https://SECRET", 429, "SECRET", {},
                                                     BytesIO(b'{"parameters":{"retry_after":17}}'))
        with self.assertRaises(APIError) as result:
            transport.request("https://example.org", "VK")
        self.assertEqual(result.exception.retry_after, 17)
        self.assertTrue(result.exception.retryable)
        self.assertNotIn("SECRET", str(result.exception))

    def test_photo_upload_contract_and_access_key(self):
        transport = Mock()
        transport.request.return_value = {"photo": "photo-payload", "server": 1, "hash": "upload-hash"}
        vk = VK("SECRET", transport=transport)
        vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/photo"},
                                   [{"id": 2, "owner_id": -1, "access_key": "key"}]])
        self.assertEqual(vk.upload_photo(2000000007, b"JPEG"), "photo-1_2_key")
        fields = vk.call.call_args.kwargs
        self.assertEqual(fields, {"photo": "photo-payload", "server": 1, "hash": "upload-hash"})
        self.assertNotIn(b"SECRET", transport.request.call_args.args[2])
        self.assertIn(b'name="photo"', transport.request.call_args.args[2])

    def test_only_vk_upload_405_is_transient(self):
        for service, status, retryable in [("VK upload", 405, True), ("VK", 405, False),
                                           ("Telegram", 405, False), ("VK upload", 403, False),
                                           ("VK upload", 415, False)]:
            with self.subTest(service=service, status=status):
                transport = Transport()
                transport.opener = Mock()
                transport.opener.open.side_effect = HTTPError(
                    "https://upload.vk.com/?SECRET", status, "SECRET", {}, BytesIO(b"SECRET"))
                with self.assertRaises(APIError) as error:
                    transport.request("https://upload.vk.com/?SECRET", service, b"multipart")
                self.assertEqual(error.exception.retryable, retryable)
                self.assertNotIn("SECRET", str(error.exception))

    def test_send_disables_mentions_and_previews(self):
        vk = VK("token")
        vk.call = Mock()
        vk.send(CFG.vk_peer_id, "hello", ["photo-1_2"], 101)
        params = vk.call.call_args.kwargs
        self.assertEqual(params["disable_mentions"], 1)
        self.assertEqual(params["dont_parse_links"], 1)
        self.assertEqual(params["random_id"], 101)

    def test_photo_size_limit_checked_before_download(self):
        tg = Telegram("123:token", Mock())
        tg.call = Mock(return_value={"file_size": MAX_PHOTO_BYTES + 1})
        with self.assertRaises(APIError):
            tg.photo("file")
        tg.transport.request.assert_not_called()


class ConfigAndCheckTests(unittest.TestCase):
    def test_env_literals_and_secret_repr(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('HASHTAG=#важно\nVK_ACCESS_TOKEN="abc$literal"\n', encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                values = read_env(path)
                self.assertEqual(values["HASHTAG"], "#важно")
                self.assertEqual(values["VK_ACCESS_TOKEN"], "abc$literal")
        self.assertNotIn("vk-test-secret", repr(CFG))
        self.assertNotIn("123:test", repr(CFG))

    def test_missing_config_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "TELEGRAM_BOT_TOKEN"):
                Config.load(Path(temp) / "missing", "run")

    def test_check_never_sends_and_rejects_privacy_mode(self):
        tg, vk = Mock(), Mock()
        tg.call.side_effect = [{"id": 1}, {}, {"type": "supergroup"}, {"status": "member"}]
        with self.assertRaisesRegex(ConfigError, "Privacy Mode"):
            check(CFG, tg, vk)
        vk.send.assert_not_called()

    def test_check_rejects_wrong_destination(self):
        tg, vk = Mock(), Mock()
        tg.call.side_effect = [{"id": 1}, {}, {"type": "supergroup"}, {"status": "administrator"}]
        vk.call.return_value = {"items": [{"peer": {"id": 2000000008, "type": "chat"}}]}
        with self.assertRaisesRegex(ConfigError, "Беседа VK не найдена"):
            check(CFG, tg, vk)

    def test_successful_check_only_reads(self):
        tg, vk = Mock(), Mock()
        tg.call.side_effect = [{"id": 1}, {}, {"type": "supergroup"}, {"status": "administrator"}]
        conversation = {"peer": {"id": CFG.vk_peer_id, "type": "chat"}, "can_write": {"allowed": True}}
        vk.call.side_effect = [{"items": [conversation]}, {"upload_url": "https://upload.vk.com"}]
        self.assertEqual(check(CFG, tg, vk)[2], conversation)
        vk.send.assert_not_called()
        tg.updates.assert_not_called()


if __name__ == "__main__":
    unittest.main()
