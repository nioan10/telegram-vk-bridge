from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from bridge.api import APIError, MAX_DOCUMENT_BYTES, Telegram, Transport, VK, document_filename
from bridge.content import extract, render
from bridge.service import deliver
from test_bridge import CFG, StoreFixture, private_update, update


def document(file_id="file", **extra):
    return dict(file_id=file_id, file_name="Отчёт за ноябрь.pdf", file_size=128, **extra)


class DocumentTests(StoreFixture):
    def test_private_document_preserves_caption_bytes_and_reuses_upload_on_retry(self):
        self.store.ingest([private_update(text="", caption="Подпись", document=document())], CFG, now=101)
        tg, vk = Mock(), Mock(); content = b"%PDF-1.7\n\x00\xff\r\n"
        tg.document.return_value = content; vk.upload_document.return_value = "doc-1_8_key"
        vk.send.side_effect = [APIError("VK", "NETWORK", True), 42]
        with self.assertRaises(APIError):
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        self.store.close(); self.store = type(self.store)(self.directory)
        deliver(self.store.due(now=103)[0], self.store, CFG, tg, vk)
        vk.upload_document.assert_called_once_with(CFG.vk_peer_id, content, "Отчёт за ноябрь.pdf")
        tg.document.assert_called_once_with("file")
        self.assertIn("Подпись", vk.send.call_args.args[1])
        self.assertEqual(vk.send.call_args.args[2], ["doc-1_8_key"])
        self.assertEqual(vk.send.call_args_list[0], vk.send.call_args_list[1])
        self.assertEqual(self.store.db.execute("SELECT body FROM jobs").fetchone()[0], "[]")

    def test_document_album_filter_and_partial_upload_retry(self):
        self.store.ingest([update(1, text="", document=document("a"), media_group_id="album"),
                           update(2, document=document("b"), media_group_id="album")], CFG, now=101)
        tg, vk = Mock(), Mock(); tg.document.return_value = b"bytes"
        vk.upload_document.side_effect = ["doc-1_1", APIError("VK upload", "HTTP_405", True), "doc-1_2"]
        with self.assertRaises(APIError):
            deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        vk.send.assert_not_called()
        deliver(self.store.due(now=110)[0], self.store, CFG, tg, vk)
        self.assertEqual([call.args[0] for call in tg.document.call_args_list], ["a", "b", "b"])
        self.assertEqual(vk.send.call_args.args[2], ["doc-1_1", "doc-1_2"])

    def test_documents_follow_filter_and_owner_scope(self):
        self.store.ingest([update(1, text="", document=document()),
                           private_update(2, text="", sender=88, document=document())], CFG, now=101)
        self.assertFalse(self.store.due(now=102))
        self.store.ingest([private_update(3, "/tag off"), update(4, text="", document=document())], CFG, now=102)
        self.assertEqual(len(self.store.due(now=103)), 1)

    def test_metadata_minimization_and_animation_compatibility(self):
        item = extract(update(text="", document=document(thumbnail={"file_id":"private"}, mime_type="text/evil"),
                              animation={"file_id":"file"})["message"], replace(CFG, hashtag="off"))
        self.assertEqual(set(item["document"]), {"file_id", "file_name", "file_size"})
        self.assertTrue(item["selected"])
        self.assertEqual(item["unsupported"], [])
        self.assertEqual(render([item], replace(CFG, include_author=False)), ("", []))

    def test_oversized_document_rejected_before_any_download_or_send(self):
        metadata = document(); metadata["file_size"] = MAX_DOCUMENT_BYTES + 1
        self.store.ingest([private_update(text="", document=metadata)], CFG, now=101)
        tg, vk = Mock(), Mock()
        with self.assertRaisesRegex(APIError, "DOCUMENT_TOO_LARGE"):
            deliver(self.store.due(now=102)[0], self.store, CFG, tg, vk)
        tg.document.assert_not_called(); vk.upload_document.assert_not_called(); vk.send.assert_not_called()


class DocumentAPITests(unittest.TestCase):
    def test_filename_sanitization_preserves_extension_and_unicode(self):
        self.assertEqual(document_filename('C:\\secret\\Отчёт.pdf'), 'Отчёт.pdf')
        self.assertEqual(document_filename('../../a"\r\nX-Test: x.pdf'), 'a___X-Test: x.pdf')
        self.assertEqual(document_filename('file\u202eevil.pdf'), 'file_evil.pdf')
        self.assertEqual(document_filename('..'), 'file.bin')
        self.assertEqual(document_filename(None), 'file.bin')
        name = document_filename('я' * 150 + '.docx')
        self.assertEqual(len(name), 128); self.assertTrue(name.endswith('.docx'))

    def test_multipart_document_contract_and_binary_integrity(self):
        content = b'PK\x03\x04\x00\xff\r\nfile bytes'
        transport = Mock(); transport.request.return_value = {"file":"uploaded"}
        vk = VK('SECRET', transport=transport)
        vk.call = Mock(side_effect=[{"upload_url":"https://upload.vk.com/docs"},
                                   {"type":"doc","doc":{"owner_id":-1,"id":5,"access_key":"key"}}])
        self.assertEqual(vk.upload_document(CFG.vk_peer_id, content, 'Архив.zip'), 'doc-1_5_key')
        call = transport.request.call_args
        mime = call.args[3]['Content-Type']
        envelope = BytesParser(policy=default).parsebytes(('Content-Type: '+mime+'\r\n\r\n').encode()+call.args[2])
        parts = list(envelope.iter_parts())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), 'Архив.zip')
        self.assertEqual(parts[0].get_payload(decode=True), content)
        self.assertEqual(parts[0].get_content_type(), 'application/octet-stream')
        self.assertEqual(vk.call.call_args.kwargs, {'file':'uploaded','title':'Архив.zip'})

    def test_download_checks_both_declared_and_received_size(self):
        transport = Mock(); tg = Telegram('SECRET', transport)
        tg.call = Mock(return_value={'file_path':'documents/file.pdf','file_size':MAX_DOCUMENT_BYTES+1})
        with self.assertRaisesRegex(APIError, 'DOCUMENT_TOO_LARGE'): tg.document('file')
        transport.request.assert_not_called()
        tg.call.return_value = {'file_path':'documents/file.pdf'}
        tg.document('file')
        self.assertEqual(transport.request.call_args.kwargs['limit'], MAX_DOCUMENT_BYTES)
        transport = Transport(); transport.opener = Mock()
        transport.opener.open.return_value = BytesIO(b'x' * (MAX_DOCUMENT_BYTES + 1))
        tg.transport = transport
        with self.assertRaisesRegex(APIError, 'RESPONSE_TOO_LARGE'): tg.document('file')

    def test_maximum_document_upload_stays_bounded_and_has_no_disk_dependency(self):
        content = b'x' * MAX_DOCUMENT_BYTES
        transport = Mock(); transport.request.return_value = {'file':'u'}
        vk = VK('SECRET', transport=transport)
        vk.call = Mock(side_effect=[{'upload_url':'https://upload.vk.com/docs'},
                                   {'doc':{'id':1,'owner_id':-1}}])
        vk.upload_document(CFG.vk_peer_id, content, 'large.bin')
        self.assertLess(len(transport.request.call_args.args[2]), MAX_DOCUMENT_BYTES+1024)
        vk.call.reset_mock()
        with self.assertRaisesRegex(APIError, 'DOCUMENT_TOO_LARGE'):
            vk.upload_document(CFG.vk_peer_id, content+b'x', 'large.bin')
        vk.call.assert_not_called()
