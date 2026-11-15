import argparse
from datetime import datetime, timezone
import getpass
import logging
import os
from pathlib import Path
import sys
import time

from . import __version__
from .api import APIError, Telegram, VK
from .config import Config, ConfigError
from .commands import filter_description
from .service import check, run
from .state import Store, process_lock, read_status


def init_env(path):
    if path.exists():
        raise ConfigError("Файл уже существует. Измените его вручную; init не перезаписывает настройки.")
    if not sys.stdin.isatty():
        raise ConfigError("init требует интерактивный терминал (для Docker используйте -it).")
    print("Ключи вводятся скрыто и сохраняются только в .env.")
    tg = getpass.getpass("Токен Telegram из BotFather: ").strip()
    vk = getpass.getpass("Ключ сообщества VK: ").strip()
    if not tg or not vk or any(c.isspace() for c in tg + vk):
        raise ConfigError("Ключи не должны быть пустыми или содержать пробелы.")
    template = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    text = template.replace("TELEGRAM_BOT_TOKEN=", "TELEGRAM_BOT_TOKEN=" + tg, 1)
    text = text.replace("VK_ACCESS_TOKEN=", "VK_ACCESS_TOKEN=" + vk, 1)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(".env создан. Выполните discover-telegram, discover-owner и discover-vk, затем впишите ID в .env.")


def discover_telegram(cfg, tg, owner=False):
    with process_lock(cfg.data_dir):
        bot = tg.call("getMe")
        if tg.call("getWebhookInfo").get("url"):
            raise ConfigError("Бот занят webhook-интеграцией. Создайте для связки отдельного бота.")
        destination = "в личку этому боту со своего аккаунта" if owner else "в нужный чат Telegram"
        print(f"В течение 60 секунд отправьте /bridge_id@{bot['username']} {destination}.", flush=True)
        offset, deadline, started = 0, time.monotonic() + 60, int(time.time())
        while time.monotonic() < deadline:
            updates = tg.updates(offset, timeout=10)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat = message.get("chat", {})
                command = (message.get("text", "").split() or [""])[0]
                allowed_type = chat.get("type") == "private" if owner else chat.get("type") in ("group", "supergroup")
                if (allowed_type and message.get("date", 0) >= started
                        and command in ("/bridge_id", f"/bridge_id@{bot['username']}")):
                    if owner:
                        print(f"Аккаунт: {message.get('from', {}).get('first_name', '')}")
                        print(f"TELEGRAM_OWNER_ID={message['from']['id']}")
                    else:
                        print(f"Чат: {chat.get('title', '(без названия)')}")
                        print(f"TELEGRAM_CHAT_ID={chat['id']}")
                    return
        raise ConfigError("Команда не получена. Проверьте, что бот добавлен в чат, и повторите discovery.")


def discover_vk(cfg, vk):
    found = 0
    for offset in range(0, 1000, 100):
        result = vk.call("messages.getConversations", count=100, offset=offset, filter="all")
        items = result.get("items", [])
        for item in items:
            conversation = item["conversation"]
            peer = conversation["peer"]
            if peer.get("type") == "chat":
                found += 1
                title = conversation.get("chat_settings", {}).get("title", "(без названия)")
                print(f"VK_PEER_ID={peer['id']}  {title}")
        if len(items) < 100:
            break
    if not found:
        raise ConfigError("Беседы не найдены. Добавьте сообщество в беседу, напишите там сообщение "
                          "с упоминанием сообщества и повторите команду.")
    print("Возьмите ID именно нужной беседы. ID из адресной строки личного аккаунта не подходит.")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Telegram → VK: сообщения с выбранным хештегом")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("command", choices=("init", "discover-telegram", "discover-owner", "discover-vk", "check", "run",
                                            "status", "retry-failed", "healthcheck"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Credentials and full API responses are never logged, including on unexpected errors.
    try:
        if args.command == "init":
            init_env(args.env_file)
            return 0
        cfg = Config.load(args.env_file, args.command)
        if args.command in ("status", "healthcheck"):
            counts, meta, failures = read_status(cfg.data_dir)
            if args.command == "healthcheck":
                return 0 if time.time() - int(meta.get("heartbeat", 0)) < 180 else 1
            print("Очередь:", counts or "пока пуста")
            print("Фильтр общего чата:", filter_description(meta.get("hashtag", cfg.hashtag)))
            if meta.get("last_sent"):
                print("Последняя доставка (UTC):", datetime.fromtimestamp(int(meta["last_sent"]), timezone.utc).isoformat())
            for job_id, error in failures:
                print("Требует внимания:", job_id, error)
            return 0
        if args.command == "retry-failed":
            with process_lock(cfg.data_dir):
                store = Store(cfg.data_dir)
                try:
                    print("Возвращено в очередь:", store.retry_failed())
                finally:
                    store.close()
            return 0
        tg = Telegram(cfg.telegram_token)
        vk = VK(cfg.vk_token, cfg.vk_version)
        if args.command in ("discover-telegram", "discover-owner"):
            discover_telegram(cfg, tg, owner=args.command == "discover-owner")
        elif args.command == "discover-vk":
            discover_vk(cfg, vk)
        elif args.command == "check":
            bot, chat, conversation = check(cfg, tg, vk)
            print(f"Telegram: @{bot['username']} / {chat.get('title')} / {cfg.telegram_chat_id}")
            print(f"VK: {conversation.get('chat_settings', {}).get('title')} / {cfg.vk_peer_id}")
            print(f"Личная отправка: пользователь {cfg.telegram_owner_id}" if cfg.private_inbox_enabled else "Личная отправка отключена.")
            print("Доступы проверены. Проверка ничего не отправляла. Можно запускать run.")
        elif args.command == "run":
            run(cfg, tg, vk)
        return 0
    except (ConfigError, APIError) as error:
        logging.error("%s", error)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.error("Внутренняя ошибка (%s). Очередь на диске сохранена.", type(error).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main())
