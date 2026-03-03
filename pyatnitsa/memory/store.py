"""Система памяти Пятница.ai — SQLite storage."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

from pyatnitsa.core.models import Fact, ConversationRecord

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, key)
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    messages TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, created_at DESC);
"""


class MemoryStore:
    """Хранилище памяти на SQLite."""
    
    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
    
    async def init(self):
        """Инициализация БД."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("memory_initialized", db_path=self.db_path)
    
    async def close(self):
        if self._db:
            await self._db.close()
    
    # ─── Facts (долгосрочная память) ─────────────────────────
    
    async def set_fact(self, user_id: str, key: str, value: str, source: str | None = None):
        """Устанавливает/обновляет факт о пользователе."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """INSERT INTO facts (user_id, key, value, source, created_at, updated_at) 
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value=?, source=?, updated_at=?""",
            (user_id, key, value, source, now, now, value, source, now),
        )
        await self._db.commit()
    
    async def get_fact(self, user_id: str, key: str) -> str | None:
        """Получает факт о пользователе."""
        cursor = await self._db.execute(
            "SELECT value FROM facts WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None
    
    async def get_all_facts(self, user_id: str) -> list[Fact]:
        """Получает все факты о пользователе."""
        cursor = await self._db.execute(
            "SELECT * FROM facts WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            Fact(
                id=row["id"],
                user_id=row["user_id"],
                key=row["key"],
                value=row["value"],
                source=row["source"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]
    
    async def delete_fact(self, user_id: str, key: str):
        """Удаляет факт."""
        await self._db.execute(
            "DELETE FROM facts WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        await self._db.commit()
    
    # ─── Conversations (история разговоров) ──────────────────
    
    async def save_conversation(self, user_id: str, channel: str, messages: list[dict], summary: str | None = None):
        """Сохраняет разговор."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO conversations (user_id, channel, messages, summary, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, channel, json.dumps(messages, ensure_ascii=False), summary, now),
        )
        await self._db.commit()
    
    async def get_recent_conversations(self, user_id: str, limit: int = 5) -> list[ConversationRecord]:
        """Получает последние разговоры."""
        cursor = await self._db.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            ConversationRecord(
                id=row["id"],
                user_id=row["user_id"],
                channel=row["channel"],
                messages=json.loads(row["messages"]),
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
    
    # ─── Контекст для LLM ───────────────────────────────────
    
    async def build_context(self, user_id: str) -> str:
        """Строит контекст памяти для инъекции в промпт."""
        parts = []
        
        # Факты
        facts = await self.get_all_facts(user_id)
        if facts:
            facts_text = "\n".join(f"- {f.key}: {f.value}" for f in facts)
            parts.append(f"Известные факты о пользователе:\n{facts_text}")
        
        # Саммари прошлых разговоров
        convos = await self.get_recent_conversations(user_id, limit=3)
        summaries = [c.summary for c in convos if c.summary]
        if summaries:
            parts.append(f"Итоги прошлых разговоров:\n" + "\n".join(f"- {s}" for s in summaries))
        
        return "\n\n".join(parts) if parts else ""
