import re


HELP = (
    "Напиши или перешли мне текст, фото или фотоальбом — отправлю в настроенную беседу VK. "
    "В личке хештег не нужен. Видео, голосовые и файлы пока не переносятся.\n\n"
    "/tag — текущий фильтр общего чата\n"
    "/tag off — пересылать все новые сообщения общего чата\n"
    "/tag #срочно — изменить хештег общего чата\n"
    "/status — состояние очереди\n"
    "/bridge_id — твой Telegram ID\n"
    "/help — эта подсказка\n\n"
    "Сообщения, начинающиеся с /, считаются командами и не отправляются в VK."
)


def filter_description(tag):
    return "все новые сообщения, без фильтра по хештегу" if tag == "off" else f"сообщения с {tag}"


def owner_command(text, current_tag, owner_id, counts):
    """Return (new hashtag or None, reply). Called only after owner authentication."""
    words = text.split()
    command = words[0].split("@", 1)[0].casefold()
    if command == "/tag":
        if len(words) == 1:
            return None, f"Фильтр общего чата: {filter_description(current_tag)}\nДля изменения: /tag #срочно или /tag off"
        if len(words) != 2 or (words[1].casefold() != "off" and not re.fullmatch(r"#\w+", words[1], re.UNICODE)):
            return None, "Нужен один хештег без пробелов или off. Пример: /tag #срочно или /tag off. Текущая настройка не изменена."
        tag = "off" if words[1].casefold() == "off" else words[1]
        return tag, (f"Сохранено. Из общего чата будут уходить {filter_description(tag)}. "
                          "Уже принятые в очередь сообщения обработаются по прежнему правилу. "
                          "Личная отправка работает без хештега.")
    if command == "/status":
        return None, (f"Бот работает. Фильтр общего чата: {filter_description(current_tag)}\n"
                      f"В очереди: {counts.get('pending', 0)}\n"
                      f"Требуют исправления: {counts.get('failed', 0)}\n"
                      f"Доставлено заданий: {counts.get('done', 0)}")
    if command == "/bridge_id":
        return None, f"Твой Telegram ID: {owner_id}"
    return None, HELP
