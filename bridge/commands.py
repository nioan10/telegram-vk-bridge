import re


HELP = (
    "Напиши или перешли мне текст, фото или фотоальбом — отправлю в настроенную беседу VK. "
    "В личке хештег не нужен. Видео, голосовые и файлы пока не переносятся.\n\n"
    "/tag — текущий хештег общего чата\n"
    "/tag #срочно — изменить хештег общего чата\n"
    "/status — состояние очереди\n"
    "/bridge_id — твой Telegram ID\n"
    "/help — эта подсказка\n\n"
    "Сообщения, начинающиеся с /, считаются командами и не отправляются в VK."
)


def owner_command(text, current_tag, owner_id, counts):
    """Return (new hashtag or None, reply). Called only after owner authentication."""
    words = text.split()
    command = words[0].split("@", 1)[0].casefold()
    if command == "/tag":
        if len(words) == 1:
            return None, f"Хештег общего чата: {current_tag}\nДля изменения: /tag #срочно"
        if len(words) != 2 or not re.fullmatch(r"#\w+", words[1], re.UNICODE):
            return None, "Нужен один хештег без пробелов. Пример: /tag #срочно. Текущая настройка не изменена."
        return words[1], (f"Сохранено. Из общего чата будут уходить новые сообщения с {words[1]}. "
                          "Уже принятые в очередь сообщения обработаются по прежнему правилу. "
                          "Личная отправка работает без хештега.")
    if command == "/status":
        return None, (f"Бот работает. Хештег общего чата: {current_tag}\n"
                      f"В очереди: {counts.get('pending', 0)}\n"
                      f"Требуют исправления: {counts.get('failed', 0)}\n"
                      f"Доставлено заданий: {counts.get('done', 0)}")
    if command == "/bridge_id":
        return None, f"Твой Telegram ID: {owner_id}"
    return None, HELP
