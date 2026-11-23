from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
import threading
import unittest
from unittest.mock import Mock

from bridge.api import APIError, MAX_VOICE_BYTES, Telegram, Transport, VK
from bridge.content import extract, render
from bridge.service import deliver
from test_bridge import CFG, StoreFixture, private_update, update


def voice(**extra):
    return {"file_id": "voice-file", "file_size": 128, "mime_type": "audio/ogg", **extra}


class VoiceDeliveryTests(StoreFixture):
    def test_voice_survives_restart_after_lost_send_response_without_reupload(self):
        message = private_update(text="", caption="Послушайте", voice=voice(),
                                 forward_origin={"sender_user_name": "Изначальный автор"})
        self.store.ingest([message], CFG, now=101)
        tg, vk = Mock(), Mock()
        content = b"OggS\x00\xff\r\nOpusHead"
        tg.voice.return_value = content
        vk.upload_voice.return_value = "doc-1_8_key"
        vk.send.side_effect = [APIError("VK", "NETWORK", True), 42]
        with self.assertRaises(APIError):
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        self.store.close(); self.store = type(self.store)(self.directory)
        self.store.ingest([message], CFG, now=103)
        deliver(self.store.due(now=103)[0], self.store, CFG, tg, vk)
        tg.voice.assert_called_once_with("voice-file")
        vk.upload_voice.assert_called_once_with(CFG.vk_peer_id, content, "audio/ogg")
        self.assertEqual(vk.send.call_args_list[0], vk.send.call_args_list[1])
        self.assertEqual(vk.send.call_args.args[2], ["doc-1_8_key"])
        self.assertIn("Изначальный автор", vk.send.call_args.args[1])
        self.assertIn("Послушайте", vk.send.call_args.args[1])
        self.assertNotIn("Не перенесены", vk.send.call_args.args[1])
        self.assertEqual(self.store.db.execute("SELECT body FROM jobs").fetchone()[0], "[]")

    def test_voice_without_caption_follows_filter_and_owner_permissions(self):
        self.store.ingest([update(1, text="", voice=voice()), private_update(2, text="", sender=88, voice=voice()),
                           private_update(3, text="", voice=voice(), has_protected_content=True)], CFG, now=101)
        self.assertFalse(self.store.due(now=102))
        self.store.ingest([private_update(4, "/tag off"), update(5, text="", voice=voice()),
                           private_update(6, text="", voice=voice())], CFG, now=102)
        self.assertEqual(len(self.store.due(now=103)), 2)

    def test_caption_hashtag_and_voice_only_without_author(self):
        message = update(text="", caption="#важно", caption_entities=[
            {"type": "hashtag", "offset": 0, "length": 6}], voice=voice())["message"]
        message.pop("text")
        item = extract(message, CFG)
        self.assertTrue(item["selected"])
        cfg = replace(CFG, include_author=False)
        self.store.ingest([private_update(text="", voice=voice())], cfg, now=101)
        tg, vk = Mock(), Mock(); tg.voice.return_value = b"OggS"; vk.upload_voice.return_value = "doc-1_8"
        deliver(self.store.due(now=102)[0], self.store, cfg, tg, vk)
        self.assertEqual(vk.send.call_args.args[1:3], ("", ["doc-1_8"]))

    def test_size_limit_prevents_download(self):
        self.store.ingest([private_update(text="", voice=voice(file_size=MAX_VOICE_BYTES+1))], CFG, now=101)
        tg, vk = Mock(), Mock()
        with self.assertRaisesRegex(APIError, "VOICE_TOO_LARGE"):
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        tg.voice.assert_not_called(); vk.upload_voice.assert_not_called(); vk.send.assert_not_called()

    def test_stop_before_voice_upload_keeps_job_pending(self):
        self.store.ingest([private_update(text="", voice=voice())], CFG, now=101)
        stop = threading.Event(); stop.set(); tg, vk = Mock(), Mock()
        deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk, stop)
        tg.voice.assert_not_called(); vk.send.assert_not_called()
        self.assertEqual(len(self.store.due(now=103)), 1)

    def test_voice_metadata_is_minimal_and_audio_files_are_still_distinct(self):
        item = extract(update(text="", voice=voice(file_unique_id="private", duration=5))["message"], replace(CFG, hashtag="off"))
        self.assertEqual(set(item["voice"]), {"file_id", "file_size", "mime_type"})
        self.assertEqual(item["unsupported"], [])
        self.assertEqual(render([item], replace(CFG, include_author=False)), ("", []))
        audio = extract(update(text="", audio={"file_id": "song"})["message"], CFG)
        self.assertEqual(audio["unsupported"], ["аудио"])

    def test_upload_failure_retries_before_sending(self):
        self.store.ingest([private_update(text="", voice=voice())], CFG, now=101)
        tg, vk = Mock(), Mock(); tg.voice.return_value = b"OggS"
        vk.upload_voice.side_effect = [APIError("VK upload", "HTTP_405", True), "doc-1_8"]
        job = self.store.due(now=102)[0]
        with self.assertRaises(APIError) as error:
            deliver(job, self.store, CFG, tg, vk)
        self.store.failure(job, error.exception, now=102)
        vk.send.assert_not_called(); self.assertFalse(self.store.due(now=103))
        deliver(self.store.due(now=104)[0], self.store, CFG, tg, vk)
        vk.send.assert_called_once()


class VoiceAPITests(unittest.TestCase):
    def test_native_voice_upload_contract_and_binary_integrity(self):
        for mime, filename, content_type in [(None, "voice.ogg", "audio/ogg"), ("audio/mpeg", "voice.mp3", "audio/mpeg"),
                                              ("audio/mp4", "voice.m4a", "audio/mp4"), ("x\r\nInjected: y", "voice.ogg", "audio/ogg")]:
            with self.subTest(mime=mime):
                content = b"OggS\x00\xff\r\nOpusHead"
                transport = Mock(); transport.request.return_value = {"file": "uploaded"}
                vk = VK("SECRET", transport=transport)
                vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/voice"},
                                           {"type": "audio_message", "audio_message": {"owner_id": -1, "id": 8, "access_key": "key"}}])
                self.assertEqual(vk.upload_voice(CFG.vk_peer_id, content, mime), "doc-1_8_key")
                self.assertEqual(vk.call.call_args_list[0].kwargs, {"peer_id": CFG.vk_peer_id, "type": "audio_message"})
                call = transport.request.call_args
                envelope = BytesParser(policy=default).parsebytes(("Content-Type: " + call.args[3]["Content-Type"] + "\r\n\r\n").encode() + call.args[2])
                part, = envelope.iter_parts()
                self.assertEqual(part.get_param("name", header="Content-Disposition"), "file")
                self.assertEqual(part.get_filename(), filename)
                self.assertEqual(part.get_content_type(), content_type)
                self.assertEqual(part.get_payload(decode=True), content)

    def test_ordinary_document_response_does_not_silently_replace_voice(self):
        for saved in [{"doc": {"id": 1, "owner_id": -1}}, {"audio_message": None}, {"audio_message": {"id": 1}}]:
            vk = VK("SECRET", transport=Mock())
            vk.transport.request.return_value = {"file": "uploaded"}
            vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/voice"}, saved])
            with self.assertRaisesRegex(APIError, "INVALID_VOICE_SAVE"):
                vk.upload_voice(CFG.vk_peer_id, b"OggS")

    def test_download_checks_declared_and_actual_size_and_upload_is_bounded(self):
        tg = Telegram("SECRET", Mock())
        tg.call = Mock(return_value={"file_path": "voice/file.oga", "file_size": MAX_VOICE_BYTES+1})
        with self.assertRaisesRegex(APIError, "VOICE_TOO_LARGE"): tg.voice("file")
        tg.transport.request.assert_not_called()
        tg.call.return_value = {"file_path": "voice/file.oga"}
        tg.voice("file")
        self.assertEqual(tg.transport.request.call_args.kwargs["limit"], MAX_VOICE_BYTES)
        transport = Transport(); transport.opener = Mock()
        transport.opener.open.return_value = BytesIO(b"x" * (MAX_VOICE_BYTES+1))
        tg.transport = transport
        with self.assertRaisesRegex(APIError, "RESPONSE_TOO_LARGE"): tg.voice("file")
        vk = VK("SECRET", transport=Mock()); vk.call = Mock()
        with self.assertRaisesRegex(APIError, "VOICE_TOO_LARGE"): vk.upload_voice(CFG.vk_peer_id, b"x" * (MAX_VOICE_BYTES+1))
        with self.assertRaisesRegex(APIError, "EMPTY_VOICE"): vk.upload_voice(CFG.vk_peer_id, b"")
        vk.call.assert_not_called()
