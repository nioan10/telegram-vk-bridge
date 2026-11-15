from .api import MAX_PHOTO_BYTES, APIError


UNSUPPORTED = {
    "video": "видео", "animation": "GIF", "document": "файл",
    "voice": "голосовое сообщение", "audio": "аудио", "video_note": "видеокружок",
    "sticker": "стикер", "poll": "опрос", "contact": "контакт",
    "location": "геопозиция", "venue": "место", "paid_media": "платное медиа",
    "story": "история", "dice": "игровой эмодзи",
}


def entity_text(text, entity):
    data = text.encode("utf-16-le")
    start = entity.get("offset", 0) * 2
    end = start + entity.get("length", 0) * 2
    return data[start:end].decode("utf-16-le")


def extract(message, config):
    """Store only fields required to forward, not the entire private update."""
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") if "text" in message else message.get("caption_entities")
    entities = entities or []
    selected = any(e.get("type") == "hashtag"
                   and entity_text(text, e).casefold() == config.hashtag.casefold()
                   for e in entities)
    # Entity offsets are UTF-16 units; apply changes backwards to preserve offsets.
    encoded = text.encode("utf-16-le")
    for e in sorted(entities, key=lambda e: e["offset"], reverse=True):
        start = e["offset"] * 2
        end = start + e["length"] * 2
        if (config.strip_hashtag and e["type"] == "hashtag"
                and entity_text(text, e).casefold() == config.hashtag.casefold()):
            encoded = encoded[:start] + encoded[end:]
        elif e["type"] == "text_link" and e.get("url"):
            encoded = encoded[:end] + f" ({e['url']})".encode("utf-16-le") + encoded[end:]
    origin = message.get("forward_origin", {})
    origin_user = origin.get("sender_user", {})
    original_author = (origin.get("sender_user_name") or origin.get("chat", {}).get("title")
                       or origin.get("sender_chat", {}).get("title")
                       or " ".join(filter(None, (origin_user.get("first_name"), origin_user.get("last_name")))))
    author = ("Переслано от: " + original_author) if original_author else message.get("sender_chat", {}).get("title")
    if not author:
        sender = message.get("from", {})
        author = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name"))))
    photos = message.get("photo", [])
    photo = max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0)) if photos else None
    return {
        "id": message["message_id"], "selected": selected,
        "text": encoded.decode("utf-16-le").strip(),
        "author": author or "Участник Telegram", "photo": photo,
        "unsupported": [label for key, label in UNSUPPORTED.items() if key in message],
    }


def split_text(text, limit=3500):
    """Use a conservative UTF-16 length for VK, without breaking emoji."""
    chunks, current, size = [], [], 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += units
    if current:
        chunks.append("".join(current))
    return chunks


def render(items, config):
    items = sorted(items, key=lambda item: item["id"])
    texts = [item["text"] for item in items if item["text"]]
    missing = sorted({label for item in items for label in item["unsupported"]})
    if missing:
        texts.append("[Не перенесены вложения: " + ", ".join(missing) + ".]")
    text = "\n\n".join(texts)
    if config.include_author:
        text = items[0]["author"] + " (Telegram)\n" + text
    if not text.strip() and not any(item["photo"] for item in items):
        text = "[Сообщение отмечено хештегом.]"
    photos = [item["photo"] for item in items if item["photo"]]
    if any(photo.get("file_size", 0) > MAX_PHOTO_BYTES for photo in photos):
        raise APIError("Telegram", "PHOTO_TOO_LARGE")
    return text.strip(), photos
