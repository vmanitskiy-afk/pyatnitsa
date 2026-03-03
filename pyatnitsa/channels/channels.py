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
                if response and (response.text or response.attachments):
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
        self._bot_username = None
        self._bot_id = None
        
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
            m = event.message
            text = m.body.text if m.body else None
            mid = m.body.mid if m.body else str(uuid.uuid4())
            chat_id = str(m.recipient.chat_id) if m.recipient else "0"
            user_id = str(m.sender.user_id) if m.sender else "unknown"

            # Кеш bot info
            if self._bot_username is None:
                try:
                    me = await self._bot.get_me()
                    self._bot_username = (me.username or "").lower()
                    self._bot_id = me.user_id
                except Exception:
                    self._bot_username = ""
                    self._bot_id = 0

            # Фильтрация в групповых чатах
            addressed = True
            try:
                from maxapi.enums.chat_type import ChatType
                is_group = m.recipient and m.recipient.chat_type == ChatType.CHAT
            except Exception:
                is_group = False

            if is_group:
                txt = text or ""
                is_command = txt.startswith("/")
                is_mention = self._bot_username and f"@{self._bot_username}" in txt.lower()
                # В MAX нет reply_to как в TG, проверяем только команды и упоминания
                addressed = is_command or is_mention

            # Убираем @mention из текста
            clean_text = text or ""
            if self._bot_username and f"@{self._bot_username}" in clean_text.lower():
                import re
                clean_text = re.sub(f"@{re.escape(self._bot_username)}", "", clean_text, flags=re.IGNORECASE).strip()

            sender_name = ""
            if m.sender:
                sender_name = getattr(m.sender, "first_name", "") or ""
                ln = getattr(m.sender, "last_name", "") or ""
                if ln:
                    sender_name = f"{sender_name} {ln}".strip()

            msg = Message(
                id=mid,
                channel=self.name,
                user_id=user_id,
                chat_id=chat_id,
                text=clean_text,
                listen_only=not addressed,
                role=MessageRole.USER,
                raw={"sender_name": sender_name},
            )
            await self._dispatch(msg)
        
        logger.info("max_channel_starting", polling=self.use_polling)
        
        if self.use_polling:
            await self._dp.start_polling(self._bot)
    
    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
    
    async def send(self, chat_id: str, response: Response):
        if not self._bot:
            return
        if response.text:
            await self._bot.send_message(chat_id=int(chat_id), text=response.text)
        for att in response.attachments:
            try:
                if att.data:
                    from io import BytesIO
                    buf = BytesIO(att.data)
                    buf.name = att.filename or "file"
                    await self._bot.send_file(chat_id=int(chat_id), file=buf)
                elif att.url and att.url.startswith("/") and hasattr(self, '_file_store') and self._file_store:
                    url_parts = att.url.split("/")
                    if len(url_parts) >= 4:
                        result = await self._file_store.get_file_data(url_parts[3])
                        if result:
                            from io import BytesIO
                            buf = BytesIO(result[0])
                            buf.name = result[2]
                            await self._bot.send_file(chat_id=int(chat_id), file=buf)
            except Exception as e:
                logger.error("max_send_file_error", error=str(e))


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
        
        # Кеш username бота
        self._bot_username = None
        self._bot_id = None

        @self._dp.message()
        async def on_message(tg_message: types.Message):
            # Кешируем info бота (один раз)
            if self._bot_username is None:
                me = await self._bot.me()
                self._bot_username = (me.username or "").lower()
                self._bot_id = me.id

            # В группах отвечаем только на команды, @упоминания и реплаи
            if tg_message.chat.type in ("group", "supergroup"):
                txt = tg_message.text or ""
                is_command = txt.startswith("/")
                is_reply_to_bot = (
                    tg_message.reply_to_message
                    and tg_message.reply_to_message.from_user
                    and tg_message.reply_to_message.from_user.id == self._bot_id
                )
                is_mention = self._bot_username and f"@{self._bot_username}" in txt.lower()
                if not is_mention and tg_message.entities:
                    for ent in tg_message.entities:
                        if ent.type == "mention":
                            m_text = txt[ent.offset:ent.offset + ent.length].lower()
                            if m_text == f"@{self._bot_username}":
                                is_mention = True
                                break
                if not (is_command or is_reply_to_bot or is_mention):
                    addressed = False
                else:
                    addressed = True
            else:
                addressed = True

            # Убираем @mention из текста
            raw_text = tg_message.text or tg_message.caption or ""
            clean_text = raw_text.replace(f"@{self._bot_username}", "").strip() if self._bot_username else raw_text

            from pyatnitsa.core.models import Attachment
            attachments = []
            tg_file = tg_message.document or (tg_message.photo[-1] if tg_message.photo else None)
            if tg_file:
                try:
                    file_info = await self._bot.get_file(tg_file.file_id)
                    result = await self._bot.download_file(file_info.file_path)
                    data = result.read() if hasattr(result, "read") else result
                    fname = getattr(tg_file, "file_name", None) or f"tg_{tg_file.file_id}"
                    mime = getattr(tg_file, "mime_type", None)
                    att_type = "image" if tg_message.photo else "file"
                    attachments.append(Attachment(type=att_type, data=data, filename=fname, mime_type=mime))
                except Exception as e:
                    logger.warning("tg_download_error", error=str(e))
            msg = Message(
                id=str(tg_message.message_id),
                channel=self.name,
                user_id=str(tg_message.from_user.id) if tg_message.from_user else "unknown",
                chat_id=str(tg_message.chat.id),
                text=clean_text,
                attachments=attachments,
                listen_only=not addressed,
                raw={"sender_name": ((tg_message.from_user.first_name or "") + " " + (tg_message.from_user.last_name or "")).strip() if tg_message.from_user else ""},
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
        if not self._bot:
            return
        if response.text:
            text = response.text
            while text:
                chunk, text = text[:4096], text[4096:]
                await self._bot.send_message(chat_id=int(chat_id), text=chunk)
        # Отправка вложений
        for att in response.attachments:
            try:
                from aiogram.types import FSInputFile, BufferedInputFile
                if att.data:
                    f = BufferedInputFile(att.data, filename=att.filename or "file")
                elif att.url and att.url.startswith("/"):
                    # Локальный файл через file_store
                    from pathlib import Path
                    path = Path(att.url.lstrip("/"))
                    if not path.exists() and hasattr(self, '_file_store') and self._file_store:
                        url_parts = att.url.split("/")
                        if len(url_parts) >= 4:
                            result = await self._file_store.get_file_data(url_parts[3])
                            if result:
                                f = BufferedInputFile(result[0], filename=result[2])
                            else:
                                continue
                    elif path.exists():
                        f = FSInputFile(str(path), filename=att.filename)
                    else:
                        continue
                else:
                    continue
                if att.type == "image" or (att.mime_type or "").startswith("image/"):
                    await self._bot.send_photo(chat_id=int(chat_id), photo=f)
                else:
                    await self._bot.send_document(chat_id=int(chat_id), document=f)
            except Exception as e:
                logger.error("tg_send_file_error", error=str(e))
