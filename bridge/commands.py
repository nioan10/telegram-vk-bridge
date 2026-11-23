import re


HELP = (
    "Напиши или перешли мне текст, фото, альбом, стикер, файл или голосовое — отправлю в настроенную беседу VK. "
    "В личке хештег не нужен. Файлы — до 20 МБ каждый, с исходным именем и подписью. "
    "Голосовые до 20 МБ приходят голосовыми VK с плеером. "
    "Видео и музыку можно отправить как файл; отдельные видеосообщения пока не переносятся.\n\n"
    "Стикеры приходят картинками, анимированные и видеостикеры — GIF.\n\n"
    "/tag — текущий фильтр общего чата\n"
    "/tag off — пересылать все новые сообщения общего чата\n"
    "/tag #срочно — изменить хештег общего чата\n"
    "/status — состояние очереди\n"
    "/retry — повторить первое задание с постоянной ошибкой\n"
    "/skip ID — явно пропустить это задание (ID из /status)\n"
    "/bridge_id — твой Telegram ID\n"
    "/help — эта подсказка\n\n"
    "Сообщения, начинающиеся с /, считаются командами и не отправляются в VK."
)


def filter_description(tag):
    return "все новые сообщения, без фильтра по хештегу" if tag == "off" else f"сообщения с {tag}"


def owner_command(text, current_tag, owner_id, counts, head=None):
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
        waiting = ""
        if head is not None:
            if head["status"] == "failed":
                waiting = (f"\nОчередь остановлена: {head['id']}\nПричина: {head['error']}\n"
                           f"После исправления: /retry\nПропустить: /skip {head['id']}")
            elif head["error"]:
                waiting = f"\nОжидает повтора: {head['id']} ({head['error']}). Следующие сообщения ждут."
            else:
                waiting = f"\nПервое задание: {head['id']}. Сообщения отправляются по порядку поступления."
        return None, (f"Бот работает. Фильтр общего чата: {filter_description(current_tag)}\n"
                      f"В очереди: {counts.get('pending', 0)}\n"
                      f"Требуют исправления: {counts.get('failed', 0)}\n"
                      f"Доставлено заданий: {counts.get('done', 0)}" + waiting)
    if command == "/bridge_id":
        return None, f"Твой Telegram ID: {owner_id}"
    return None, HELP
