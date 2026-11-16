import json
import logging
import signal
import threading
import time

from .api import APIError
from .config import ConfigError
from .content import render, split_text
from .stickers import convert_sticker
from .state import Store, process_lock


def send_command_replies(store, cfg, tg):
    for reply in store.due_replies():
        try:
            tg.call("sendMessage", chat_id=cfg.telegram_owner_id, text=reply["text"],
                    reply_parameters=json.dumps({"message_id": reply["message_id"], "allow_sending_without_reply": True}))
            store.finish_reply(reply["message_id"])
            logging.info("Ответ на команду доставлен: %s", reply["message_id"])
        except APIError as error:
            logging.warning("Не удалось отправить ответ на команду: %s", error)
            if error.retryable:
                store.retry_reply(reply, error)
                break
            store.finish_reply(reply["message_id"], delivered=False)


def check(cfg, tg, vk):
    bot = tg.call("getMe")
    if tg.call("getWebhookInfo").get("url"):
        raise ConfigError("У Telegram-бота уже настроен webhook. Используйте отдельного бота для этой связки.")
    chat = tg.call("getChat", chat_id=cfg.telegram_chat_id)
    if chat.get("type") not in ("group", "supergroup"):
        raise ConfigError("Источником должна быть группа Telegram.")
    if chat.get("has_protected_content"):
        raise ConfigError("В чате включён запрет пересылки. Согласуйте его изменение с администратором.")
    member = tg.call("getChatMember", chat_id=cfg.telegram_chat_id, user_id=bot["id"])
    if member.get("status") in ("left", "kicked") or member.get("is_member") is False:
        raise ConfigError("Telegram-бот не является участником исходного чата.")
    if member.get("status") not in ("administrator", "creator") and not bot.get("can_read_all_group_messages"):
        raise ConfigError("Назначьте Telegram-бота администратором или отключите Privacy Mode и добавьте его заново.")
    result = vk.call("messages.getConversationsById", peer_ids=str(cfg.vk_peer_id))
    conversation = next((item for item in result.get("items", [])
                         if item.get("peer", {}).get("id") == cfg.vk_peer_id), None)
    if not conversation or conversation.get("peer", {}).get("type") != "chat":
        raise ConfigError("Беседа VK не найдена для этого ключа сообщества. Повторите discover-vk.")
    if conversation.get("can_write", {}).get("allowed") is False:
        raise ConfigError("Сообщество не может отправлять сообщения в выбранную беседу VK.")
    if conversation.get("chat_settings", {}).get("state") in ("left", "kicked"):
        raise ConfigError("Сообщество удалено из беседы VK.")
    vk.call("photos.getMessagesUploadServer", peer_id=cfg.vk_peer_id)
    return bot, chat, conversation


def deliver(job, store, cfg, tg, vk, stop=None):
    if not job["selected"]:
        store.finish(job["id"], "ignored")
        return
    plan = json.loads(job["plan"]) if job["plan"] else None
    if plan is None:
        items = json.loads(job["body"])
        text, photos = render(items, cfg)
        attachments = []
        for photo in photos:
            if stop and stop.is_set():
                return
            store.heartbeat()
            attachment = store.upload(job["id"], photo["file_id"])
            if not attachment:
                content = tg.photo(photo["file_id"])
                store.heartbeat()
                attachment = vk.upload_photo(cfg.vk_peer_id, content)
                store.upload(job["id"], photo["file_id"], attachment)
            attachments.append(attachment)
        for item in items:
            sticker = item.get("sticker")
            if not sticker:
                continue
            if stop and stop.is_set():
                return
            store.heartbeat()
            key = "sticker:" + sticker["file_id"]
            attachment = store.upload(job["id"], key)
            if not attachment:
                content, kind = convert_sticker(tg.sticker(sticker["file_id"]), sticker)
                store.heartbeat()
                attachment = (vk.upload_gif(cfg.vk_peer_id, content) if kind == "gif"
                              else vk.upload_photo(cfg.vk_peer_id, content, png=True))
                store.upload(job["id"], key, attachment)
            attachments.append(attachment)
        for item in items:
            document = item.get("document")
            if not document:
                continue
            if stop and stop.is_set():
                return
            store.heartbeat()
            key = "document:" + document["file_id"]
            attachment = store.upload(job["id"], key)
            if not attachment:
                content = tg.document(document["file_id"])
                store.heartbeat()
                attachment = vk.upload_document(cfg.vk_peer_id, content, document.get("file_name"))
                store.upload(job["id"], key, attachment)
            attachments.append(attachment)
        chunks = split_text(text) or [""]
        plan = [{"text": chunk, "attachments": attachments[:10] if i == 0 else []}
                for i, chunk in enumerate(chunks)]
        for i in range(10, len(attachments), 10):
            plan.append({"text": "", "attachments": attachments[i:i+10]})
        # Persist the exact payload and its deduplication IDs BEFORE the first send.
        plan = store.save_plan(job["id"], plan)
    for i in range(job["cursor"], len(plan)):
        if stop and stop.is_set():
            return
        store.heartbeat()
        part = plan[i]
        vk.send(cfg.vk_peer_id, part["text"], part["attachments"], part["random_id"])
        store.advance(job["id"], i + 1)
    store.finish(job["id"])
    logging.info("Доставлено: %s", job["id"])
    if job["id"].startswith(f"{cfg.telegram_owner_id}:"):
        try:
            items = json.loads(job["body"])
            has_missing = any(item["unsupported"] for item in items)
            tg.call("sendMessage", chat_id=cfg.telegram_owner_id,
                    text=("Отправлено в VK. Неподдерживаемые вложения пропущены; в копии есть пометка."
                          if has_missing else "Отправлено в VK."),
                    reply_parameters=json.dumps({"message_id": items[0]["id"], "allow_sending_without_reply": True}))
        except APIError as error:
            # The VK delivery is already committed. Never resend it because an acknowledgement failed.
            logging.warning("VK доставлено, подтверждение в Telegram недоступно: %s", error)


def run(cfg, tg, vk):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with process_lock(cfg.data_dir):
        bot, _, _ = check(cfg, tg, vk)
        store = Store(cfg.data_dir)
        try:
            store.start(cfg, bot["id"])
            logging.info("Бот запущен: Telegram %s → VK %s, фильтр %s",
                         cfg.telegram_chat_id, cfg.vk_peer_id, store.get("hashtag"))
            next_poll = 0
            next_delivery = 0
            while not stop.is_set():
                store.heartbeat()
                updates = []
                if time.monotonic() >= next_poll:
                    try:
                        updates = tg.updates(store.offset)
                        store.ingest(updates, cfg)
                    except APIError as error:
                        if not error.retryable:
                            raise
                        logging.warning("Приём временно недоступен: %s", error)
                        next_poll = time.monotonic() + max(5, error.retry_after)
                send_command_replies(store, cfg, tg)
                # Drain full batches before closing albums that straddle batches.
                if len(updates) < 100 and time.monotonic() >= next_delivery:
                    for job in store.due():
                        if stop.is_set():
                            break
                        try:
                            deliver(job, store, cfg, tg, vk, stop)
                        except APIError as error:
                            store.failure(job, error)
                            logging.warning("Доставка %s: %s (%s)", job["id"], error,
                                            "повтор запланирован" if error.retryable else "нужно исправить доступ/вложение и выполнить retry-failed")
                            if error.retryable and error.service.startswith("VK"):
                                next_delivery = time.monotonic() + max(
                                    error.retry_after, min(300, 2 ** min(job["attempts"] + 1, 9)))
                                break
                stop.wait(0.2)
        finally:
            store.close()
            logging.info("Бот остановлен; очередь сохранена.")
