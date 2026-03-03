"""Базовые модели данных Пятница.ai"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ─── Сообщения ───────────────────────────────────────────────

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Attachment(BaseModel):
    """Вложение к сообщению."""
    type: str  # "image", "file", "voice", "video"
    url: str | None = None
    data: bytes | None = None
    filename: str | None = None
    mime_type: str | None = None


class Message(BaseModel):
    """Универсальное сообщение (channel-agnostic)."""
    id: str
    channel: str  # "max", "telegram", "api"
    user_id: str
    chat_id: str
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    role: MessageRole = MessageRole.USER
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)  # оригинал от канала


class Response(BaseModel):
    """Ответ агента."""
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─── Tool Calls ──────────────────────────────────────────────

class ToolCall(BaseModel):
    """Вызов инструмента (навыка) от LLM."""
    id: str
    skill_name: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Результат выполнения инструмента."""
    tool_call_id: str
    success: bool = True
    result: str = ""
    error: str | None = None


# ─── Память ──────────────────────────────────────────────────

class Fact(BaseModel):
    """Факт о пользователе (long-term memory)."""
    id: int | None = None
    user_id: str
    key: str  # "role", "team", "timezone", etc.
    value: str
    source: str | None = None  # "conversation:123", "onboarding"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationRecord(BaseModel):
    """Запись разговора в памяти."""
    id: int | None = None
    user_id: str
    channel: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Events (для heartbeat/scheduler) ───────────────────────

class EventType(str, Enum):
    DEADLINE_APPROACHING = "deadline_approaching"
    TASK_OVERDUE = "task_overdue"
    NEW_EMAIL = "new_email"
    DEAL_STATUS_CHANGED = "deal_status_changed"
    CUSTOM = "custom"


class Event(BaseModel):
    """Событие от интеграции или планировщика."""
    type: EventType
    source: str  # "redmine", "bitrix", "email"
    user_id: str | None = None
    title: str
    description: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
