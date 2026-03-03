"""Каналы связи — абстракция над мессенджерами."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog

from pyatnitsa.core.models import Message, Response, MessageRole

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# Тип обработчика сообщений
MessageHandler = Callable[[Message], Awaitable[Response | None]]


class BaseChannel(ABC):
    """Абстрактный канал связи."""
    
    name: str = "base"
    
    def __init__(self):
        self._handler: MessageHandler | None = None
    
    def on_message(self, handler: MessageHandler):
        """Регистрирует обработчик входящих сообщений."""
        self._handler = handler
    
    @abstractmethod
    async def start(self):
        """Запускает канал (polling/webhook)."""
        ...
    
    @abstractmethod
    async def stop(self):
        """Останавливает канал."""
        ...
    
    @abstractmethod
    async def send(self, chat_id: str, response: Response):
        """Отправляет ответ в чат."""
        ...
    
    async def _dispatch(self, message: Message):
        """Передаёт сообщение обработчику."""
        if self._handler:
            try:
                response = await self._handler(message)
                if response:
                    await self.send(message.chat_id, response)
            except Exception as e:
                logger.error("channel_dispatch_error", channel=self.name, error=str(e))
                await self.send(message.chat_id, Response(text="⚠️ Произошла ошибка. Попробуйте позже."))


# ─── MAX Messenger ───────────────────────────────────────────

class MaxChannel(BaseChannel):
    """Канал MAX мессенджер (через max-botapi-python)."""
    
    name = "max"
    
    def __init__(self, token: str, use_polling: bool = True):
        super().__init__()
        self.token = token
        self.use_polling = use_polling
        self._bot = None
        self._dp = None
    
    async def start(self):
        """Запускает MAX бот через polling или webhook."""
        try:
            from maxapi import Bot, Dispatcher
            from maxapi.types import MessageCreated, BotStarted
        except ImportError:
            logger.error("maxapi_not_installed", hint="pip install git+https://github.com/max-messenger/max-botapi-python.git")
            return
        
        self._bot = Bot(self.token)
        self._dp = Dispatcher()
        
        @self._dp.bot_started()
        async def on_start(event: BotStarted):
            msg = Message(
                id=str(uuid.uuid4()),
                channel=self.name,
                user_id=str(event.user.user_id),
                chat_id=str(event.chat_id),
                text="/start",
                role=MessageRole.USER,
            )
            await self._dispatch(msg)
        
        @self._dp.message_created()
        async def on_message(event: MessageCreated):
            text = None
            if event.message and event.message.body:
                text = event.message.body.text if hasattr(event.message.body, "text") else str(event.message.body)
            
            msg = Message(
                id=str(event.message.message_id) if event.message else str(uuid.uuid4()),
                channel=self.name,
                user_id=str(event.message.sender.user_id) if event.message and event.message.sender else "unknown",
                chat_id=str(event.chat_id),
                text=text,
                role=MessageRole.USER,
            )
            await self._dispatch(msg)
        
        logger.info("max_channel_starting", polling=self.use_polling)
        
        if self.use_polling:
            await self._dp.start_polling(self._bot)
    
    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
    
    async def send(self, chat_id: str, response: Response):
        if self._bot and response.text:
            await self._bot.send_message(chat_id=int(chat_id), text=response.text)


# ─── Telegram ────────────────────────────────────────────────

class TelegramChannel(BaseChannel):
    """Канал Telegram (через aiogram 3)."""
    
    name = "telegram"
    
    def __init__(self, token: str, use_polling: bool = True):
        super().__init__()
        self.token = token
        self.use_polling = use_polling
        self._bot = None
        self._dp = None
    
    async def start(self):
        from aiogram import Bot, Dispatcher, types
        
        self._bot = Bot(token=self.token)
        self._dp = Dispatcher()
        
        @self._dp.message()
        async def on_message(tg_message: types.Message):
            msg = Message(
                id=str(tg_message.message_id),
                channel=self.name,
                user_id=str(tg_message.from_user.id) if tg_message.from_user else "unknown",
                chat_id=str(tg_message.chat.id),
                text=tg_message.text or "",
                role=MessageRole.USER,
            )
            await self._dispatch(msg)
        
        logger.info("telegram_channel_starting", polling=self.use_polling)
        
        if self.use_polling:
            await self._dp.start_polling(self._bot)
    
    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
        if self._bot:
            await self._bot.session.close()
    
    async def send(self, chat_id: str, response: Response):
        if self._bot and response.text:
            # Разбиваем длинные сообщения
            text = response.text
            while text:
                chunk, text = text[:4096], text[4096:]
                await self._bot.send_message(chat_id=int(chat_id), text=chunk)
