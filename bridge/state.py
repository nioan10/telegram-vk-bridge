from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

from .config import ConfigError
from .content import extract
from .commands import owner_command


@contextmanager
def process_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bridge.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("Уже запущен другой экземпляр бота с этим DATA_DIR. Сначала остановите его.") from None
        try:
            yield
        finally:
            if __import__("os").name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "bridge.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, selected INTEGER NOT NULL,
                due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                plan TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, due);
            CREATE TABLE IF NOT EXISTS uploads (
                job_id TEXT NOT NULL, file_id TEXT NOT NULL, attachment TEXT NOT NULL,
                PRIMARY KEY (job_id, file_id)
            );
            CREATE TABLE IF NOT EXISTS command_replies (
                message_id INTEGER PRIMARY KEY, text TEXT NOT NULL,
                due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0
            );
        """)
        # Retry deadlines and album debounce must never change a message's place.
        # Older databases retain their original insertion order during migration.
        with self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "queue_order" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN queue_order INTEGER")
            order = self.db.execute("SELECT COALESCE(MAX(queue_order),0) FROM jobs").fetchone()[0]
            missing = self.db.execute("SELECT id FROM jobs WHERE queue_order IS NULL ORDER BY rowid").fetchall()
            for row in missing:
                order += 1
                self.db.execute("UPDATE jobs SET queue_order=? WHERE id=?", (order, row["id"]))
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_order ON jobs(queue_order)")
            self.db.execute("CREATE INDEX IF NOT EXISTS jobs_unfinished_order ON jobs(queue_order) WHERE status IN ('pending','failed')")

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def start(self, cfg, bot_id, now=None):
        binding = json.dumps([cfg.telegram_chat_id, cfg.vk_peer_id, bot_id, cfg.telegram_owner_id,
                              hashlib.sha256(cfg.vk_token.encode()).hexdigest()])
        with self.db:
            previous = self.get("binding")
            if previous and previous != binding:
                raise ConfigError("Эта база привязана к другим чатам, владельцу или ключу VK. "
                                  "Не меняйте маршрут существующей очереди; используйте новый DATA_DIR.")
            self.put("binding", binding)
            if self.get("started_at") is None:
                self.put("started_at", int(now if now is not None else time.time()))
            if self.get("hashtag") is None:
                self.put("hashtag", cfg.hashtag)

    @property
    def offset(self):
        return int(self.get("offset", 0))

    def ingest(self, updates, cfg, now=None):
        now = now if now is not None else time.time()
        cutoff = int(self.get("started_at", int(now)))
        replies = []
        with self.db:
            for update in sorted(updates, key=lambda item: int(item["update_id"])):
                if int(update["update_id"]) < self.offset:
                    continue
                message = update.get("message", {})
                chat = message.get("chat", {})
                private = (cfg.private_inbox_enabled and cfg.telegram_owner_id > 0
                           and chat.get("type") == "private"
                           and chat.get("id") == cfg.telegram_owner_id
                           and message.get("from", {}).get("id") == cfg.telegram_owner_id)
                group = (chat.get("id") == cfg.telegram_chat_id
                         and chat.get("type") in ("group", "supergroup"))
                command = private and message.get("text", "").startswith("/")
                current_cfg = replace(cfg, hashtag=self.get("hashtag", cfg.hashtag))
                if command and message.get("date", 0) >= cutoff:
                    counts = dict(self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
                    queue_reply = self.queue_command(message["text"])
                    new_tag, reply = (None, queue_reply) if queue_reply is not None else owner_command(
                        message["text"], current_cfg.hashtag, cfg.telegram_owner_id, counts, self.queue_head())
                    if new_tag:
                        self.put("hashtag", new_tag)
                    replies.append({"message_id": message["message_id"], "text": reply})
                    self.db.execute("INSERT OR IGNORE INTO command_replies(message_id,text) VALUES(?,?)",
                                    (message["message_id"], reply))
                if ((group or private) and not command
                        and message.get("date", 0) >= cutoff
                        and not message.get("has_protected_content")):
                    album = message.get("media_group_id")
                    job_id = f"{chat['id']}:" + (f"album:{album}" if album else f"msg:{message['message_id']}")
                    existing = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if existing and (existing["status"] != "pending" or existing["plan"]):
                        # A very late album item never causes the already sent album to repeat.
                        if album:
                            import logging
                            logging.warning("Поздняя часть уже обработанного альбома: %s", job_id)
                    else:
                        item = extract(message, current_cfg)
                        if private:
                            item["selected"] = True
                        if album or item["selected"]:
                            items = json.loads(existing["body"]) if existing else []
                            if not any(old["id"] == item["id"] for old in items):
                                items.append(item)
                                selected = any(part["selected"] for part in items)
                                order = existing["queue_order"] if existing else self.db.execute(
                                    "SELECT COALESCE(MAX(queue_order),0)+1 FROM jobs").fetchone()[0]
                                self.db.execute("""INSERT INTO jobs(id,body,selected,due,queue_order) VALUES(?,?,?,?,?)
                                    ON CONFLICT(id) DO UPDATE SET body=excluded.body,
                                    selected=excluded.selected,due=excluded.due""",
                                    (job_id, json.dumps(items, ensure_ascii=False), int(selected),
                                     now + cfg.album_wait if album else now, order))
                self.put("offset", int(update["update_id"]) + 1)
            self.put("last_poll", int(now))
        return replies

    def due_replies(self, now=None):
        return self.db.execute("SELECT * FROM command_replies WHERE due<=? ORDER BY message_id LIMIT 10",
                               (time.time() if now is None else now,)).fetchall()

    def finish_reply(self, message_id, delivered=True):
        with self.db:
            self.db.execute("DELETE FROM command_replies WHERE message_id=?", (message_id,))
            if delivered:
                self.put("last_reply", int(time.time()))

    def retry_reply(self, reply, error, now=None):
        now = time.time() if now is None else now
        attempts = reply["attempts"] + 1
        delay = max(error.retry_after, min(300, 2 ** min(attempts, 9)))
        with self.db:
            self.db.execute("UPDATE command_replies SET attempts=?,due=? WHERE message_id=?",
                            (attempts, now + delay, reply["message_id"]))

    def queue_head(self):
        return self.db.execute("SELECT * FROM jobs WHERE status IN ('pending','failed') ORDER BY queue_order LIMIT 1").fetchone()

    def due(self, now=None):
        now = now if now is not None else time.time()
        jobs = self.db.execute("SELECT * FROM jobs WHERE status IN ('pending','failed') ORDER BY queue_order LIMIT 10").fetchall()
        ready = []
        for job in jobs:
            if job["status"] != "pending" or job["due"] > now:
                break
            ready.append(job)
        return ready

    def queue_command(self, text):
        """Owner-only commands, inside ingest's transaction with its update checkpoint."""
        words = text.split()
        command = words[0].split("@", 1)[0].casefold()
        if command not in ("/retry", "/skip"):
            return None
        if command == "/retry" and len(words) != 1:
            return "Используйте /retry без параметров: повторится первое задание с постоянной ошибкой."
        if command == "/skip" and len(words) != 2:
            return "Укажите ID остановившего очередь задания: /skip ID. ID есть в /status."
        head = self.queue_head()
        if head is None or head["status"] != "failed":
            return "В начале очереди нет задания с постоянной ошибкой. Временные ошибки повторяются автоматически."
        if command == "/retry":
            self.db.execute("UPDATE jobs SET status='pending',due=0,attempts=0,error=NULL WHERE id=?", (head["id"],))
            return f"Задание {head['id']} возвращено на отправку. Следующие сообщения ждут его завершения."
        if words[1] != head["id"]:
            return "Это не первое остановившее очередь задание. Проверьте его ID через /status."
        self.db.execute("UPDATE jobs SET status='skipped',body='[]',plan=NULL,error=NULL WHERE id=?", (head["id"],))
        self.db.execute("DELETE FROM uploads WHERE job_id=?", (head["id"],))
        return f"Неотправленная часть задания {head['id']} пропущена. Очередь продолжит работу."

    def upload(self, job_id, file_id, attachment=None):
        if attachment is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO uploads VALUES(?,?,?)", (job_id, file_id, attachment))
        row = self.db.execute("SELECT attachment FROM uploads WHERE job_id=? AND file_id=?", (job_id, file_id)).fetchone()
        return row[0] if row else None

    def save_plan(self, job_id, parts):
        with self.db:
            next_id = int(self.get("next_random_id", secrets.randbelow(1_000_000_000) + 1))
            if next_id + len(parts) >= 2_147_483_647:
                raise ConfigError("Исчерпан диапазон random_id. Нужна новая база очереди.")
            for part in parts:
                part["random_id"] = next_id
                next_id += 1
            self.db.execute("UPDATE jobs SET plan=? WHERE id=?", (json.dumps(parts, ensure_ascii=False), job_id))
            self.put("next_random_id", next_id)
        return parts

    def advance(self, job_id, cursor):
        with self.db:
            self.db.execute("UPDATE jobs SET cursor=? WHERE id=?", (cursor, job_id))

    def finish(self, job_id, status="done"):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,body='[]',plan=NULL,error=NULL WHERE id=?", (status, job_id))
            self.db.execute("DELETE FROM uploads WHERE job_id=?", (job_id,))
            if status == "done":
                self.put("last_sent", int(time.time()))

    def failure(self, job, error, now=None):
        now = now if now is not None else time.time()
        attempts = job["attempts"] + 1
        delay = max(error.retry_after, min(300, 2 ** min(attempts, 9)))
        with self.db:
            self.db.execute("UPDATE jobs SET attempts=?,error=?,due=?,status=? WHERE id=?",
                            (attempts, str(error), now + delay,
                             "pending" if error.retryable else "failed", job["id"]))

    def retry_failed(self):
        with self.db:
            return self.db.execute("UPDATE jobs SET status='pending',due=0,attempts=0,error=NULL WHERE status='failed'").rowcount

    def heartbeat(self):
        with self.db:
            self.put("heartbeat", int(time.time()))


def read_status(directory):
    path = (Path(directory) / "bridge.sqlite3").resolve()
    if not path.exists():
        raise ConfigError("База ещё не создана: сначала запустите run.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('heartbeat','last_sent','hashtag','last_poll','last_reply')"))
        failures = db.execute("SELECT id,error FROM jobs WHERE status='failed' ORDER BY due LIMIT 10").fetchall()
    return counts, meta, failures
