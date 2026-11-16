"""HTTPS clients. Exceptions contain codes, never URLs, tokens or message text."""
import http.client
import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler


MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_STICKER_BYTES = 4 * 1024 * 1024


class APIError(Exception):
    def __init__(self, service, code, retryable=False, retry_after=0):
        self.service = service
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(f"{service}: {code}")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self):
        self.opener = build_opener(NoRedirects())

    def request(self, url, service, data=None, headers=None, timeout=45,
                limit=4 * 1024 * 1024, raw=False):
        if urlsplit(url).scheme != "https":
            raise APIError(service, "HTTPS_REQUIRED")
        request = Request(url, data=data, headers=headers or {})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(limit + 1)
        except HTTPError as exc:
            # Telegram error responses carry retry_after; never show their description.
            delay = 0
            try:
                payload = json.loads(exc.read(65536))
                delay = int(payload.get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError, AttributeError, OSError):
                pass
            finally:
                exc.close()
            # VK's upload edge sometimes returns 405 for a valid multipart POST;
            # fetching a fresh upload URL and retrying the same file succeeds.
            # Keep ordinary API/method and permission errors permanent.
            upload_unavailable = service == "VK upload" and exc.code == 405
            raise APIError(service, f"HTTP_{exc.code}",
                           upload_unavailable or exc.code in (408, 429) or exc.code >= 500, delay) from None
        except (URLError, OSError, TimeoutError, http.client.HTTPException):
            raise APIError(service, "NETWORK", True) from None
        if len(body) > limit:
            raise APIError(service, "RESPONSE_TOO_LARGE")
        if raw:
            return body
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise APIError(service, "INVALID_JSON", True) from None

    def form(self, url, service, fields, timeout=45):
        return self.request(url, service, urlencode(fields).encode(),
                            {"Content-Type": "application/x-www-form-urlencoded"}, timeout)


class Telegram:
    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport or Transport()

    def call(self, method, *, request_timeout=15, **params):
        result = self.transport.form(
            f"https://api.telegram.org/bot{self.token}/{method}", "Telegram", params, timeout=request_timeout)
        if not isinstance(result, dict) or "ok" not in result:
            raise APIError("Telegram", "INVALID_RESPONSE", True)
        if not result["ok"]:
            code = result.get("error_code", 0)
            raise APIError("Telegram", code, code == 429 or code >= 500,
                           result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def updates(self, offset=0, timeout=2):
        return self.call("getUpdates", request_timeout=timeout + 8, offset=offset, timeout=timeout, limit=100,
                         allowed_updates=json.dumps(["message"]))

    def photo(self, file_id):
        return self.download(file_id, MAX_PHOTO_BYTES, "PHOTO_TOO_LARGE")

    def sticker(self, file_id):
        return self.download(file_id, MAX_STICKER_BYTES, "STICKER_TOO_LARGE")

    def download(self, file_id, limit, size_error):
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > limit:
            raise APIError("Telegram", size_error)
        path = info.get("file_path")
        if not path or ".." in path.split("/"):
            raise APIError("Telegram", "INVALID_FILE_PATH")
        return self.transport.request(
            f"https://api.telegram.org/file/bot{self.token}/{quote(path, safe='/')}",
            "Telegram file", limit=limit, raw=True)


class VK:
    def __init__(self, token, version="5.199", transport=None):
        self.token = token
        self.version = version
        self.transport = transport or Transport()
        self.last_call = 0.0

    def call(self, method, **params):
        # Stay below the community API's request rate, including photo API calls.
        time.sleep(max(0, 0.36 - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
        result = self.transport.form(f"https://api.vk.com/method/{method}", "VK",
                                     dict(params, access_token=self.token, v=self.version))
        if not isinstance(result, dict):
            raise APIError("VK", "INVALID_RESPONSE", True)
        if "error" in result:
            code = result["error"].get("error_code", 0)
            raise APIError("VK", code, code in (1, 6, 9, 10, 29),
                           3600 if code == 29 else 0)
        if "response" not in result:
            raise APIError("VK", "INVALID_RESPONSE", True)
        return result["response"]

    def upload_photo(self, peer_id, content, *, png=False):
        server = self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        url = server.get("upload_url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise APIError("VK upload", "INVALID_UPLOAD_URL")
        # URL is returned by the authenticated VK API, never taken from a chat message.
        boundary = "bridge-" + uuid.uuid4().hex
        filename, mime = ("sticker.png", "image/png") if png else ("photo.jpg", "image/jpeg")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += content + f"\r\n--{boundary}--\r\n".encode()
        uploaded = self.transport.request(url, "VK upload", body,
                                          {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if not isinstance(uploaded, dict) or not all(k in uploaded for k in ("photo", "server", "hash")):
            raise APIError("VK upload", "INVALID_UPLOAD_RESPONSE", True)
        if uploaded["photo"] in ("[]", "", None):
            raise APIError("VK upload", "EMPTY_PHOTO")
        saved = self.call("photos.saveMessagesPhoto",
                          **{key: uploaded[key] for key in ("photo", "server", "hash")})
        if not saved or "id" not in saved[0] or "owner_id" not in saved[0]:
            raise APIError("VK upload", "INVALID_SAVE_RESPONSE", True)
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        if photo.get("access_key"):
            attachment += "_" + photo["access_key"]
        return attachment

    def upload_gif(self, peer_id, content):
        server = self.call("docs.getMessagesUploadServer", peer_id=peer_id, type="doc")
        url = server.get("upload_url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise APIError("VK upload", "INVALID_UPLOAD_URL")
        boundary = "bridge-" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                'filename="sticker.gif"\r\nContent-Type: image/gif\r\n\r\n').encode()
        body += content + f"\r\n--{boundary}--\r\n".encode()
        uploaded = self.transport.request(url, "VK upload", body,
                                          {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if not isinstance(uploaded, dict) or not uploaded.get("file"):
            raise APIError("VK upload", "INVALID_DOCUMENT_RESPONSE", True)
        saved = self.call("docs.save", file=uploaded["file"], title="sticker.gif")
        doc = saved.get("doc", {}) if isinstance(saved, dict) else {}
        if "id" not in doc or "owner_id" not in doc:
            raise APIError("VK upload", "INVALID_DOCUMENT_SAVE", True)
        attachment = f"doc{doc['owner_id']}_{doc['id']}"
        if doc.get("access_key"):
            attachment += "_" + doc["access_key"]
        return attachment

    def send(self, peer_id, text, attachments, random_id):
        return self.call("messages.send", peer_id=peer_id, message=text,
                         attachment=",".join(attachments), random_id=random_id,
                         disable_mentions=1, dont_parse_links=1)
