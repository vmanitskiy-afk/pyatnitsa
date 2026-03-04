"""Трекер событий Пятница.ai — аналитика и статистика."""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger()

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    user_id TEXT,
    channel TEXT,
    metadata TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, timestamp);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    role TEXT DEFAULT 'user',
    channels TEXT DEFAULT '[]',
    first_seen REAL,
    last_seen REAL,
    message_count INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0
);
"""


class EventTracker:
    """Записывает события для аналитики и управляет профилями пользователей."""

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def init(self):
        """Создаёт таблицы если не существуют."""
        await self._db.executescript(EVENTS_SCHEMA)
        await self._db.commit()
        logger.info("event_tracker_initialized")

    # ─── Events ──────────────────────────────────────────────

    async def track(self, event_type: str, user_id: str | None = None,
                    channel: str | None = None, **meta: Any):
        """Записывает событие."""
        await self._db.execute(
            "INSERT INTO events (timestamp, event_type, user_id, channel, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), event_type, user_id, channel,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        await self._db.commit()

    async def get_stats(self, hours: float = 24) -> dict[str, Any]:
        """Сводная статистика за период."""
        since = time.time() - hours * 3600

        # Общее кол-во сообщений
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='message' AND timestamp > ?",
            (since,))
        total_messages = (await cur.fetchone())[0]

        # Сообщения по каналам
        cur = await self._db.execute(
            "SELECT channel, COUNT(*) FROM events "
            "WHERE event_type='message' AND timestamp > ? GROUP BY channel",
            (since,))
        by_channel = {row[0]: row[1] for row in await cur.fetchall()}

        # Ошибки LLM
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='llm_error' AND timestamp > ?",
            (since,))
        llm_errors = (await cur.fetchone())[0]

        # Среднее время ответа
        cur = await self._db.execute(
            "SELECT AVG(json_extract(metadata, '$.latency_ms')) FROM events "
            "WHERE event_type='llm_call' AND timestamp > ? "
            "AND json_extract(metadata, '$.latency_ms') IS NOT NULL",
            (since,))
        row = await cur.fetchone()
        avg_latency = round(row[0]) if row[0] else None

        # Общее кол-во токенов
        cur = await self._db.execute(
            "SELECT SUM(json_extract(metadata, '$.tokens')) FROM events "
            "WHERE event_type='llm_call' AND timestamp > ?",
            (since,))
        row = await cur.fetchone()
        total_tokens = row[0] or 0

        # Популярные скиллы
        cur = await self._db.execute(
            "SELECT json_extract(metadata, '$.skill'), COUNT(*) FROM events "
            "WHERE event_type='skill_call' AND timestamp > ? "
            "GROUP BY json_extract(metadata, '$.skill') ORDER BY COUNT(*) DESC LIMIT 10",
            (since,))
        top_skills = {row[0]: row[1] for row in await cur.fetchall()}

        # Уникальные пользователи
        cur = await self._db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM events "
            "WHERE event_type='message' AND timestamp > ?",
            (since,))
        unique_users = (await cur.fetchone())[0]

        # Сообщения по дням (последние 7 дней)
        cur = await self._db.execute(
            "SELECT date(created_at) as day, COUNT(*) FROM events "
            "WHERE event_type='message' AND timestamp > ? "
            "GROUP BY day ORDER BY day",
            (time.time() - 7 * 86400,))
        by_day = {row[0]: row[1] for row in await cur.fetchall()}

        return {
            "period_hours": hours,
            "total_messages": total_messages,
            "by_channel": by_channel,
            "llm_errors": llm_errors,
            "avg_latency_ms": avg_latency,
            "total_tokens": total_tokens,
            "top_skills": top_skills,
            "unique_users": unique_users,
            "by_day": by_day,
        }

    async def get_recent_events(self, limit: int = 50,
                                event_type: str | None = None) -> list[dict]:
        """Последние события (для логов)."""
        if event_type:
            cur = await self._db.execute(
                "SELECT * FROM events WHERE event_type=? ORDER BY timestamp DESC LIMIT ?",
                (event_type, limit))
        else:
            cur = await self._db.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,))
        rows = await cur.fetchall()
        return [
            {
                "id": row[0], "timestamp": row[1], "event_type": row[2],
                "user_id": row[3], "channel": row[4],
                "metadata": json.loads(row[5]) if row[5] else None,
                "created_at": row[6],
            }
            for row in rows
        ]

    # ─── Users ───────────────────────────────────────────────

    async def touch_user(self, user_id: str, channel: str | None = None,
                         display_name: str | None = None):
        """Обновляет профиль пользователя (или создаёт новый)."""
        now = time.time()
        cur = await self._db.execute(
            "SELECT channels FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()

        if row:
            channels = json.loads(row[0]) if row[0] else []
            if channel and channel not in channels:
                channels.append(channel)
            update_parts = [
                "last_seen = ?", "message_count = message_count + 1",
                "channels = ?",
            ]
            params: list[Any] = [now, json.dumps(channels)]
            if display_name:
                update_parts.append("display_name = ?")
                params.append(display_name)
            params.append(user_id)
            await self._db.execute(
                f"UPDATE users SET {', '.join(update_parts)} WHERE user_id = ?",
                params)
        else:
            channels = [channel] if channel else []
            await self._db.execute(
                "INSERT INTO users (user_id, display_name, channels, first_seen, last_seen, message_count) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (user_id, display_name or "", json.dumps(channels), now, now))
        await self._db.commit()

    async def get_users(self, include_blocked: bool = False) -> list[dict]:
        """Список пользователей."""
        query = "SELECT * FROM users"
        if not include_blocked:
            query += " WHERE blocked = 0"
        query += " ORDER BY last_seen DESC"
        cur = await self._db.execute(query)
        rows = await cur.fetchall()
        return [
            {
                "user_id": row[0], "display_name": row[1], "role": row[2],
                "channels": json.loads(row[3]) if row[3] else [],
                "first_seen": row[4], "last_seen": row[5],
                "message_count": row[6], "blocked": bool(row[7]),
            }
            for row in rows
        ]

    async def get_user(self, user_id: str) -> dict | None:
        """Профиль пользователя."""
        cur = await self._db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0], "display_name": row[1], "role": row[2],
            "channels": json.loads(row[3]) if row[3] else [],
            "first_seen": row[4], "last_seen": row[5],
            "message_count": row[6], "blocked": bool(row[7]),
        }

    async def set_user_role(self, user_id: str, role: str):
        """Назначить роль (admin/user)."""
        await self._db.execute(
            "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
        await self._db.commit()

    async def block_user(self, user_id: str, blocked: bool = True):
        """Блокировка пользователя."""
        await self._db.execute(
            "UPDATE users SET blocked = ? WHERE user_id = ?",
            (1 if blocked else 0, user_id))
        await self._db.commit()

    async def is_blocked(self, user_id: str) -> bool:
        """Проверка блокировки."""
        cur = await self._db.execute(
            "SELECT blocked FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False
