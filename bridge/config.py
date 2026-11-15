from dataclasses import dataclass, field
from pathlib import Path
import os
import re


class ConfigError(Exception):
    pass


def read_env(path: Path) -> dict[str, str]:
    """Small literal .env reader. Never executes or expands values."""
    values = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ConfigError(f"Некорректная строка {number} в .env.")
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigError(f"Незакрытые кавычки в строке {number} .env.")
                value = value[1:-1]
            values[key] = value
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    vk_token: str = field(repr=False)
    telegram_chat_id: int = 0
    vk_peer_id: int = 0
    telegram_owner_id: int = 0
    private_inbox_enabled: bool = True
    hashtag: str = "#важно"
    strip_hashtag: bool = False
    include_author: bool = True
    album_wait: int = 5
    data_dir: Path = Path("state")
    vk_version: str = "5.199"

    @classmethod
    def load(cls, path: Path, command: str):
        env = read_env(path)

        def integer(name, default):
            try:
                return int(env.get(name) or default)
            except ValueError:
                raise ConfigError(f"{name} должен быть целым числом.") from None

        def boolean(name, default):
            value = env.get(name, default).lower()
            if value not in ("true", "false"):
                raise ConfigError(f"{name}: допустимо true или false.")
            return value == "true"

        cfg = cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            vk_token=env.get("VK_ACCESS_TOKEN", ""),
            telegram_chat_id=integer("TELEGRAM_CHAT_ID", 0),
            vk_peer_id=integer("VK_PEER_ID", 0),
            telegram_owner_id=integer("TELEGRAM_OWNER_ID", 0),
            private_inbox_enabled=boolean("PRIVATE_INBOX_ENABLED", "true"),
            hashtag="off" if env.get("HASHTAG", "").casefold() == "off" else env.get("HASHTAG", "#важно"),
            strip_hashtag=boolean("STRIP_HASHTAG", "false"),
            include_author=boolean("INCLUDE_AUTHOR", "true"),
            album_wait=integer("ALBUM_WAIT_SECONDS", 5),
            data_dir=Path(env.get("DATA_DIR") or "state"),
            vk_version=env.get("VK_API_VERSION", "5.199"),
        )
        if command in ("run", "check", "discover-telegram", "discover-owner"):
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", cfg.telegram_token):
                raise ConfigError("Заполните TELEGRAM_BOT_TOKEN токеном из BotFather.")
        if command in ("run", "check", "discover-vk"):
            if not cfg.vk_token or any(c.isspace() for c in cfg.vk_token):
                raise ConfigError("Заполните VK_ACCESS_TOKEN ключом сообщества.")
        if command in ("run", "check"):
            if cfg.telegram_chat_id >= 0:
                raise ConfigError("TELEGRAM_CHAT_ID должен быть отрицательным ID группы.")
            if cfg.vk_peer_id <= 2_000_000_000:
                raise ConfigError("VK_PEER_ID должен быть ID беседы из discover-vk.")
            if cfg.private_inbox_enabled and cfg.telegram_owner_id <= 0:
                raise ConfigError("Заполните TELEGRAM_OWNER_ID через discover-owner для личной отправки.")
        if cfg.hashtag != "off" and not re.fullmatch(r"#[\w]+", cfg.hashtag, re.UNICODE):
            raise ConfigError("HASHTAG должен содержать один хештег, например #важно, или off для всех сообщений.")
        if not 2 <= cfg.album_wait <= 60:
            raise ConfigError("ALBUM_WAIT_SECONDS должен быть от 2 до 60.")
        if not re.fullmatch(r"\d+\.\d+", cfg.vk_version):
            raise ConfigError("Некорректный VK_API_VERSION.")
        return cfg
