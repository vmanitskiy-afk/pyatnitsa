"""Персистентное хранилище чатов и сообщений.

Каждое сообщение сохраняется в SQLite мгновенно.
При достижении порога — компакция: LLM суммаризует старые сообщения.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger()

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT 'Новый чат',
    summary TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    is_compacted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chats_user_active
    ON chats(user_id, is_active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat
    ON chat_messages(chat_id, is_compacted, created_at);
"""

# ─── Оценка токенов ─────────────────────────────────────────

def estimate_tokens(content: str | list | dict) -> int:
    """Грубая оценка токенов: ~4 символа = 1 токен для русского."""
    if isinstance(content, str):
        return max(1, len(content) // 3)
    text = json.dumps(content, ensure_ascii=False)
    return max(1, len(text) // 3)


# ─── Модели ──────────────────────────────────────────────────

class ChatInfo:
    """Метаданные чата."""
    __slots__ = ("id", "user_id", "channel", "title", "summary",
                 "is_active", "created_at", "updated_at", "message_count")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class ChatMessage:
    """Одно сообщение в чате."""
    __slots__ = ("id", "chat_id", "role", "content", "token_estimate",
                 "is_compacted", "created_at")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


# ─── Store ───────────────────────────────────────────────────

class ConversationStore:
    """Хранилище чатов в SQLite."""

    # Пороги компакции
    COMPACTION_THRESHOLD = 30       # сообщений до срабатывания
    COMPACTION_KEEP_RECENT = 10     # свежих сообщений оставляем как есть
    MAX_CONTEXT_TOKENS = 12_000     # макс. токенов в контексте

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def init(self):
        """Создаёт таблицы если не существуют."""
        await self._db.executescript(CHAT_SCHEMA)
        await self._db.commit()
        logger.info("conversation_store_initialized")

    # ─── Чаты ──────────────────────────────────────────────

    async def get_or_create_active_chat(
        self, user_id: str, channel: str = ""
    ) -> ChatInfo:
        """Возвращает активный чат или создаёт новый."""
        cursor = await self._db.execute(
            """SELECT c.*, COUNT(m.id) as message_count
               FROM chats c
               LEFT JOIN chat_messages m ON m.chat_id = c.id AND m.is_compacted = 0
               WHERE c.user_id = ? AND c.is_active = 1
               GROUP BY c.id
               ORDER BY c.updated_at DESC LIMIT 1""",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_chat(row)
        return await self.create_chat(user_id, channel)

    async def create_chat(
        self, user_id: str, channel: str = "", title: str = "Новый чат"
    ) -> ChatInfo:
        """Создаёт новый чат, деактивируя старый."""
        # Деактивируем предыдущие
        await self._db.execute(
            "UPDATE chats SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        now = datetime.utcnow().isoformat()
        cursor = await self._db.execute(
            "INSERT INTO chats (user_id, channel, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (user_id, channel, title, now, now),
        )
        await self._db.commit()
        chat_id = cursor.lastrowid
        logger.info("chat_created", user_id=user_id, chat_id=chat_id)
        return ChatInfo(
            id=chat_id, user_id=user_id, channel=channel, title=title,
            summary=None, is_active=True, created_at=now,
            updated_at=now, message_count=0,
        )

    async def list_chats(
        self, user_id: str, limit: int = 10
    ) -> list[ChatInfo]:
        """Список последних чатов пользователя."""
        cursor = await self._db.execute(
            """SELECT c.*, COUNT(m.id) as message_count
               FROM chats c
               LEFT JOIN chat_messages m ON m.chat_id = c.id
               WHERE c.user_id = ?
               GROUP BY c.id
               ORDER BY c.updated_at DESC LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_chat(r) for r in rows]

    async def set_chat_title(self, chat_id: int, title: str):
        """Обновляет заголовок чата."""
        await self._db.execute(
            "UPDATE chats SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title, chat_id),
        )
        await self._db.commit()

    # ─── Сообщения ─────────────────────────────────────────

    async def add_message(
        self, chat_id: int, role: str, content: str | list | dict
    ) -> int:
        """Сохраняет сообщение. Возвращает id."""
        if isinstance(content, (list, dict)):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = content
        tokens = estimate_tokens(content)
        cursor = await self._db.execute(
            """INSERT INTO chat_messages (chat_id, role, content, token_estimate)
               VALUES (?, ?, ?, ?)""",
            (chat_id, role, content_str, tokens),
        )
        # Обновляем updated_at чата
        await self._db.execute(
            "UPDATE chats SET updated_at = datetime('now') WHERE id = ?",
            (chat_id,),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_messages(
        self, chat_id: int, include_compacted: bool = False
    ) -> list[ChatMessage]:
        """Все не-компактированные сообщения чата."""
        if include_compacted:
            where = "WHERE chat_id = ?"
            params = (chat_id,)
        else:
            where = "WHERE chat_id = ? AND is_compacted = 0"
            params = (chat_id,)
        cursor = await self._db.execute(
            f"SELECT * FROM chat_messages {where} ORDER BY created_at",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def count_active_messages(self, chat_id: int) -> int:
        """Кол-во не-компактированных сообщений."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ? AND is_compacted = 0",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return row[0]

    # ─── Контекст для LLM ─────────────────────────────────

    async def build_llm_messages(self, chat_id: int) -> tuple[str | None, list[dict]]:
        """Строит (summary_prefix, messages) для отправки в LLM.

        Возвращает:
            summary_prefix: текст компактированных сообщений (или None)
            messages: список {role, content} для LLM
        """
        # Получаем summary чата
        cursor = await self._db.execute(
            "SELECT summary FROM chats WHERE id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        summary = row["summary"] if row and row["summary"] else None

        # Активные сообщения
        msgs = await self.get_messages(chat_id)

        # Обрезаем по токенам если нужно
        result = []
        total_tokens = estimate_tokens(summary) if summary else 0
        # Идём с конца — приоритет свежим
        for m in reversed(msgs):
            if total_tokens + m.token_estimate > self.MAX_CONTEXT_TOKENS:
                break
            result.append(m)
            total_tokens += m.token_estimate
        result.reverse()

        llm_messages = []
        for m in result:
            content = m.content
            # Пробуем распарсить JSON (для tool_use/tool_result блоков)
            try:
                parsed = json.loads(content)
                if isinstance(parsed, (list, dict)):
                    content = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            llm_messages.append({"role": m.role, "content": content})

        return summary, llm_messages

    # ─── Компакция ─────────────────────────────────────────

    async def needs_compaction(self, chat_id: int) -> bool:
        """Нужна ли компакция?"""
        count = await self.count_active_messages(chat_id)
        return count > self.COMPACTION_THRESHOLD

    async def compact(self, chat_id: int, llm_summarize) -> str | None:
        """Компактирует старые сообщения.

        Args:
            llm_summarize: async callable(text) -> summary_str

        Помечает старые сообщения как compacted, сохраняет summary.
        """
        msgs = await self.get_messages(chat_id)
        if len(msgs) <= self.COMPACTION_KEEP_RECENT:
            return None

        # Разделяем: старые → на компакцию, свежие → оставляем
        to_compact = msgs[:-self.COMPACTION_KEEP_RECENT]
        if not to_compact:
            return None

        # Собираем текст для суммаризации
        existing_summary = None
        cursor = await self._db.execute(
            "SELECT summary FROM chats WHERE id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row and row["summary"]:
            existing_summary = row["summary"]

        text_parts = []
        if existing_summary:
            text_parts.append(f"Предыдущее резюме разговора:\n{existing_summary}\n")
        text_parts.append("Новые сообщения для суммаризации:")
        for m in to_compact:
            role_label = "Пользователь" if m.role == "user" else "Ассистент"
            content = m.content
            # Для tool content — упрощаем
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    parts = []
                    for block in parsed:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(block["text"])
                            elif block.get("type") == "tool_use":
                                parts.append(f"[вызов {block.get('name', '?')}]")
                            elif block.get("type") == "tool_result":
                                r = block.get("content", "")
                                parts.append(f"[результат: {r[:100]}...]" if len(r) > 100 else f"[результат: {r}]")
                    content = " ".join(parts)
                elif isinstance(parsed, dict) and parsed.get("type") == "tool_result":
                    content = f"[результат инструмента]"
            except (json.JSONDecodeError, TypeError):
                pass
            text_parts.append(f"{role_label}: {content}")

        compaction_text = "\n".join(text_parts)

        # Вызываем LLM для суммаризации
        try:
            summary = await llm_summarize(compaction_text)
        except Exception as e:
            logger.error("compaction_llm_failed", chat_id=chat_id, error=str(e)[:100])
            return None

        # Помечаем старые сообщения как compacted
        ids_to_compact = [m.id for m in to_compact]
        placeholders = ",".join("?" * len(ids_to_compact))
        await self._db.execute(
            f"UPDATE chat_messages SET is_compacted = 1 WHERE id IN ({placeholders})",
            ids_to_compact,
        )

        # Сохраняем summary в чат
        await self._db.execute(
            "UPDATE chats SET summary = ?, updated_at = datetime('now') WHERE id = ?",
            (summary, chat_id),
        )
        await self._db.commit()

        logger.info("chat_compacted", chat_id=chat_id,
                     compacted=len(to_compact), summary_len=len(summary))
        return summary

    # ─── Авто-заголовок ────────────────────────────────────

    async def maybe_set_title(self, chat_id: int, llm_title) -> str | None:
        """Автоматически генерирует заголовок после первого обмена.

        Args:
            llm_title: async callable(user_msg, assistant_msg) -> title_str
        """
        cursor = await self._db.execute(
            "SELECT title FROM chats WHERE id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if not row or row["title"] != "Новый чат":
            return None  # уже есть заголовок

        msgs = await self.get_messages(chat_id)
        if len(msgs) < 2:
            return None

        user_msg = next((m.content for m in msgs if m.role == "user"), "")
        asst_msg = next((m.content for m in msgs if m.role == "assistant"), "")

        try:
            title = await llm_title(user_msg, asst_msg)
            title = title.strip().strip('"')[:80]
            await self.set_chat_title(chat_id, title)
            return title
        except Exception as e:
            logger.warning("auto_title_failed", error=str(e)[:80])
            return None

    # ─── Helpers ───────────────────────────────────────────

    def _row_to_chat(self, row) -> ChatInfo:
        return ChatInfo(
            id=row["id"], user_id=row["user_id"], channel=row["channel"],
            title=row["title"], summary=row["summary"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            message_count=row["message_count"] if "message_count" in row.keys() else 0,
        )

    def _row_to_msg(self, row) -> ChatMessage:
        return ChatMessage(
            id=row["id"], chat_id=row["chat_id"], role=row["role"],
            content=row["content"],
            token_estimate=row["token_estimate"],
            is_compacted=bool(row["is_compacted"]),
            created_at=row["created_at"],
        )
