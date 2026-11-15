from dataclasses import replace
import gzip
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from PIL import Image

from bridge.api import APIError, MAX_STICKER_BYTES, Telegram, Transport, VK
from bridge.content import extract, render
from bridge.service import deliver
from bridge.stickers import convert_sticker, MAX_JSON
from test_bridge import CFG, StoreFixture, private_update, update


def animated_fixture():
    return {"v": "5.5.2", "fr": 30, "ip": 0, "op": 30, "w": 64, "h": 64, "assets": [],
            "layers": [{"ddd": 0, "ind": 1, "ty": 4, "nm": "Square", "sr": 1,
                        "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                               "p": {"a": 1, "k": [{"t": 0, "s": [16, 32, 0], "e": [48, 32, 0],
                                      "i": {"x": 0.667, "y": 1}, "o": {"x": 0.333, "y": 0}},
                                     {"t": 30, "s": [48, 32, 0]}]},
                               "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
                        "shapes": [{"ty": "rc", "d": 1, "s": {"a": 0, "k": [16, 16]},
                                    "p": {"a": 0, "k": [0, 0]}, "r": {"a": 0, "k": 0}},
                                   {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]},
                                    "o": {"a": 0, "k": 100}, "r": 1}],
                        "ip": 0, "op": 30, "st": 0, "bm": 0}]}


class ConversionTests(unittest.TestCase):
    def test_static_webp_becomes_png_with_visible_pixels(self):
        image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        image.putpixel((16, 16), (255, 0, 0, 255))
        source = BytesIO(); image.save(source, format="WEBP", lossless=True)
        content, kind = convert_sticker(source.getvalue(), {})
        self.assertEqual(kind, "png")
        with Image.open(BytesIO(content)) as result:
            self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(result.getpixel((16, 16)), (255, 0, 0))

    def test_tgs_becomes_a_moving_gif(self):
        source = gzip.compress(json.dumps(animated_fixture()).encode())
        content, kind = convert_sticker(source, {"is_animated": True})
        self.assertEqual(kind, "gif")
        with Image.open(BytesIO(content)) as result:
            self.assertGreater(result.n_frames, 1)
            first = result.convert("RGB").tobytes()
            result.seek(result.n_frames - 1)
            self.assertNotEqual(first, result.convert("RGB").tobytes())
            self.assertLessEqual(max(result.size), 320)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for WebM conversion")
    def test_webm_becomes_animated_gif(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.webm"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=64x64:rate=15:duration=1", "-c:v", "libvpx-vp9", "-threads", "1", str(path)],
                           check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            content, kind = convert_sticker(path.read_bytes(), {"is_video": True})
        self.assertEqual(kind, "gif")
        with Image.open(BytesIO(content)) as result:
            self.assertGreater(result.n_frames, 1)

    def test_invalid_oversized_and_external_data_are_rejected(self):
        bad = animated_fixture(); bad["assets"] = [{"id": "image", "p": "secret.png", "u": "/app/"}]
        for content, metadata in [(b"not an image", {}),
                                  (gzip.compress(b"x" * (MAX_JSON + 1)), {"is_animated": True}),
                                  (gzip.compress(json.dumps(bad).encode()), {"is_animated": True})]:
            with self.assertRaises(APIError): convert_sticker(content, metadata)
        with patch("bridge.stickers.subprocess.run") as process:
            with self.assertRaisesRegex(APIError, "STICKER_TOO_LARGE"):
                convert_sticker(b"x" * (MAX_STICKER_BYTES + 1), {})
            process.assert_not_called()

    def test_worker_timeout_hides_details_and_drops_credentials(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "SECRET", "VK_ACCESS_TOKEN": "SECRET"}), \
                patch("bridge.stickers.subprocess.run", side_effect=subprocess.TimeoutExpired("SECRET", 45)) as run:
            with self.assertRaisesRegex(APIError, "CONVERSION_TIMEOUT") as error:
                convert_sticker(b"data", {})
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn("VK_ACCESS_TOKEN", run.call_args.kwargs["env"])
            self.assertNotIn("TELEGRAM_BOT_TOKEN", run.call_args.kwargs["env"])


class StickerDeliveryTests(StoreFixture):
    def test_upload_405_retries_with_fresh_url_without_losing_sticker(self):
        self.store.ingest([private_update(text="", sticker={"file_id": "test"})], CFG, now=101)
        tg = Mock(); tg.sticker.return_value = b"webp"
        transport = Transport(); transport.opener = Mock()
        transport.opener.open.side_effect = [
            HTTPError("https://upload.vk.com/old", 405, "Unavailable", {}, BytesIO(b"")),
            BytesIO(b'{"photo":"p","server":1,"hash":"h"}')]
        vk = VK("test", transport=transport)
        vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/old"},
                                   {"upload_url": "https://upload.vk.com/fresh"},
                                   [{"id": 2, "owner_id": -1}], 42])
        with patch("bridge.service.convert_sticker", return_value=(b"PNG", "png")):
            job = self.store.due(now=102)[0]
            with self.assertRaises(APIError) as error:
                deliver(job, self.store, CFG, tg, vk)
            self.store.failure(job, error.exception, now=102)
            self.assertFalse(self.store.due(now=103))
            retry = self.store.due(now=104)[0]
            self.assertEqual(retry["status"], "pending")
            self.assertIn("sticker", retry["body"])
            deliver(retry, self.store, CFG, tg, vk)
        urls = [call.args[0].full_url for call in transport.opener.open.call_args_list]
        self.assertEqual(urls, ["https://upload.vk.com/old", "https://upload.vk.com/fresh"])
        sends = [call for call in vk.call.call_args_list if call.args[0] == "messages.send"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].kwargs["attachment"], "photo-1_2")
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0], "done")

    def test_private_sticker_and_retry_reuse_uploaded_attachment(self):
        self.store.ingest([private_update(text="", sticker={"file_id": "test", "is_animated": True})], CFG, now=101)
        job = self.store.due(now=102)[0]; tg, vk = Mock(), Mock()
        tg.sticker.return_value = b"tgs"; vk.upload_gif.return_value = "doc-1_2_key"
        vk.send.side_effect = [APIError("VK", "NETWORK", True), 42]
        with patch("bridge.service.convert_sticker", return_value=(b"GIF89a", "gif")) as convert:
            with self.assertRaises(APIError): deliver(job, self.store, CFG, tg, vk)
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(vk.upload_gif.call_count, 1)
        self.assertEqual(vk.send.call_args.args[2], ["doc-1_2_key"])
        self.assertEqual(vk.send.call_args_list[0].args[3], vk.send.call_args_list[1].args[3])

    def test_stickers_follow_filter_and_source(self):
        sticker = {"file_id": "file", "is_animated": False, "emoji": "test", "set_name": "private-name"}
        self.store.ingest([update(1, text="", sticker=sticker)], CFG, now=101)
        self.assertFalse(self.store.due(now=102))
        self.store.ingest([private_update(2, "/tag off"), update(3, text="", sticker=sticker)], CFG, now=101)
        job = self.store.due(now=102)[0]
        item = json.loads(job["body"])[0]
        self.assertNotIn("set_name", item["sticker"])
        self.assertEqual(item["unsupported"], [])
        text, photos = render([item], replace(CFG, include_author=False))
        self.assertEqual((text, photos), ("", []))
        tg, vk = Mock(), Mock(); tg.sticker.return_value = b"webp"
        vk.upload_photo.return_value = "photo-1_3"
        with patch("bridge.service.convert_sticker", return_value=(b"PNG", "png")):
            deliver(job, self.store, CFG, tg, vk)
        vk.upload_photo.assert_called_once_with(CFG.vk_peer_id, b"PNG", png=True)


class StickerAPITests(unittest.TestCase):
    def test_download_checks_sticker_limit(self):
        tg = Telegram("secret", Mock()); tg.call = Mock(return_value={"file_size": MAX_STICKER_BYTES + 1})
        with self.assertRaises(APIError): tg.sticker("id")
        tg.transport.request.assert_not_called()

    def test_gif_upload_contract(self):
        transport = Mock(); transport.request.return_value = {"file": "upload-token"}
        vk = VK("SECRET", transport=transport)
        vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/docs"},
                                   {"type": "doc", "doc": {"id": 7, "owner_id": -1, "access_key": "key"}}])
        self.assertEqual(vk.upload_gif(CFG.vk_peer_id, b"GIF89a"), "doc-1_7_key")
        self.assertIn(b'name="file"', transport.request.call_args.args[2])
        self.assertIn(b"image/gif", transport.request.call_args.args[2])
        self.assertEqual(vk.call.call_args.kwargs, {"file": "upload-token", "title": "sticker.gif"})
        self.assertNotIn(b"SECRET", transport.request.call_args.args[2])

    def test_png_mime_type_and_upload_failure(self):
        transport = Mock(); transport.request.return_value = {"photo": "p", "server": 1, "hash": "h"}
        vk = VK("SECRET", transport=transport)
        vk.call = Mock(side_effect=[{"upload_url": "https://upload.vk.com/photo"}, [{"id": 1, "owner_id": -1}]])
        vk.upload_photo(CFG.vk_peer_id, b"PNG", png=True)
        self.assertIn(b"image/png", transport.request.call_args.args[2])
        transport.request.return_value = {}
        vk.call = Mock(return_value={"upload_url": "https://upload.vk.com/docs"})
        with self.assertRaisesRegex(APIError, "INVALID_DOCUMENT_RESPONSE"):
            vk.upload_gif(CFG.vk_peer_id, b"GIF")
