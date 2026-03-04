"""Agент Пятница -- главный оркестратор.

Персистентные чаты, компакция длинных диалогов, команды /new /history.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from pyatnitsa.core.models import Message, Response, Event, ToolCall, Attachment
from pyatnitsa.core.llm import LLMManager, LLMMessage, LLMTool
from pyatnitsa.core.extractor import extract_text
from pyatnitsa.skills.skills import SkillLoader
from pyatnitsa.memory.store import MemoryStore
from pyatnitsa.memory.conversations import ConversationStore

logger = structlog.get_logger()

SYSTEM_PROMPT = """Ты - Пятница, персональный AI-ассистент для бизнеса.
Ты помогаешь пользователю управлять задачами, проектами и бизнес-процессами.

Правила:
- Отвечай на русском языке
- Будь кратким и по делу
- Если нужно выполнить действие - используй доступные инструменты
- Если не знаешь ответ - скажи об этом, не выдумывай
- Запоминай важные факты о пользователе для будущих разговоров

Работа с файлами:
- Когда пользователь прикрепляет файлы, информация о них в блоках [File: name | path: /abs/path]
- Каждый файл имеет абсолютный path — используй именно его для redmine.attach(file_path=...)
- Можешь анализировать и отвечать на вопросы по содержимому файлов

{memory_context}

{summary_context}

Доступные навыки:
{skills_context}
"""

COMPACTION_PROMPT = """Сделай краткое резюме этого разговора. Сохрани:
- Ключевые факты и решения
- Какие действия были выполнены (задачи, проекты, запросы)
- Контекст который может понадобиться для продолжения разговора

Не включай приветствия, пустые фразы, технические детали tool calls.
Формат: 3-7 предложений, фактологически.

{text}"""

TITLE_PROMPT = """Придумай короткий заголовок (3-6 слов) для этого диалога.
Только заголовок, без кавычек и пояснений.

Пользователь: {user_msg}
Ассистент: {asst_msg}"""


class Agent:
    """Главный агент Пятница.ai."""

    def __init__(self, llm: LLMManager, skills: SkillLoader,
                 memory: MemoryStore, conversations: ConversationStore | None = None,
                 file_store=None):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        self.conversations = conversations
        self.file_store = file_store
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._user_last_msg: dict[str, float] = {}  # user_id -> timestamp последнего обработанного

    async def handle_message(self, message: Message) -> Response:
        user_id = message.user_id
        text = (message.text or "").strip()
        logger.info("agent_message_received", user_id=user_id,
                     channel=message.channel, text=text[:100],
                     attachments=len(message.attachments))

        # Per-user lock: один пользователь — одно сообщение за раз.
        # Дубли от MAX будут ждать в очереди, а потом отбрасываться по timestamp.
        import time as _time
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()

        async with self._user_locks[user_id]:
            now = _time.time()
            last = self._user_last_msg.get(user_id, 0)
            # Если прошлое сообщение закончило обработку <5с назад и это не команда — дубль
            if now - last < 5.0 and not text.startswith("/"):
                logger.info("agent_dedup_skip", user_id=user_id, gap=round(now - last, 2))
                return Response(text=None)
            try:
                result = await self._handle_message_inner(message, user_id, text)
                return result
            finally:
                self._user_last_msg[user_id] = _time.time()

    async def _handle_message_inner(self, message: Message, user_id: str, text: str) -> Response:

        if text.startswith("/"):
            cmd_response = await self._handle_command(user_id, text, message.channel)
            if cmd_response:
                return cmd_response

        # Обработка вложений — извлечение текста + картинок для LLM
        file_context, images = await self._process_attachments(message.attachments)
        # Краткие метки файлов для сохранения в БД
        file_labels = ""
        if message.attachments:
            names = [a.filename or "file" for a in message.attachments]
            file_labels = " ".join(f"[\U0001F4CE {n}]" for n in names)

        if not self.conversations:
            if message.listen_only:
                return Response(text=None)
            # legacy: передаём полный контент в LLM
            if file_context:
                message.text = f"{text}\n\n{file_context}" if text else file_context
            return await self._handle_legacy(message)

        conv = self.conversations
        chat = await conv.get_or_create_active_chat(user_id, message.channel)

        # В БД — чистый текст + короткие метки файлов (без содержимого!)
        sender_name = message.raw.get("sender_name", "")
        display_text = f"{text}\n{file_labels}" if file_labels else text
        save_text = f"[{sender_name}]: {display_text}" if sender_name and message.listen_only else display_text
        await conv.add_message(chat.id, "user", save_text)

        # listen_only: сохранили в историю, но не отвечаем
        if message.listen_only:
            return Response(text=None)

        memory_context = await self.memory.build_context(user_id)
        tools = self.skills.get_all_tools()
        skills_desc = "\n".join(
            f"- {s.name}: {s.description}" for s in self.skills.skills.values()
        )

        summary, llm_messages = await conv.build_llm_messages(chat.id)
        summary_block = f"Резюме предыдущей части разговора:\n{summary}" if summary else ""

        system = SYSTEM_PROMPT.format(
            memory_context=memory_context or "Пока ничего не известно.",
            summary_context=summary_block,
            skills_context=skills_desc or "Навыки не загружены.",
        )

        history = [LLMMessage(role=m["role"], content=m["content"]) for m in llm_messages]

        # Инжектируем файловый контент в последнее сообщение (только для LLM!)
        # Для картинок НЕ добавляем текстовые метки — они пойдут как image-блоки
        if file_context and history and not images:
            last = history[-1]
            if last.role == "user" and isinstance(last.content, str):
                last.content = f"{last.content}\n\n{file_context}"
        elif file_context and history and images:
            # Есть и картинки, и другие файлы — инжектируем только не-image часть
            non_image_parts = [p for p in file_context.split("\n\n") if not p.startswith("[Image:")]
            if non_image_parts:
                extra = "\n\n".join(non_image_parts)
                last = history[-1]
                if last.role == "user" and isinstance(last.content, str):
                    last.content = f"{last.content}\n\n{extra}"

        # Если есть картинки — делаем мультимодальное сообщение
        if images:
            import base64
            last_user_content = history[-1].content if history else text
            multimodal = []
            if isinstance(last_user_content, str) and last_user_content:
                multimodal.append({"type": "text", "text": last_user_content})
            for img in images:
                b64 = base64.b64encode(img["data"]).decode("utf-8")
                multimodal.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": img["mime_type"], "data": b64},
                })
            if history:
                history[-1].content = multimodal
            else:
                history.append(LLMMessage(role="user", content=multimodal))

        response_text = await self._run_agent_loop(user_id, system, history, tools, chat.id)
        await conv.add_message(chat.id, "assistant", response_text)
        await conv.maybe_set_title(chat.id, self._generate_title)

        if await conv.needs_compaction(chat.id):
            logger.info("compaction_triggered", chat_id=chat.id)
            await conv.compact(chat.id, self._summarize_for_compaction)

        return Response(text=response_text)

    async def _process_attachments(self, attachments: list):
        """Returns (text_context, images_list)."""
        if not attachments:
            return None, []
        parts = []
        images = []
        for att in attachments:
            fname = att.filename or "file"
            file_path = None

            if self.file_store and hasattr(att, "url") and att.url:
                url_parts = (att.url or "").split("/")
                if len(url_parts) >= 4 and url_parts[1] == "api" and url_parts[2] == "files":
                    meta = await self.file_store.get_file(url_parts[3])
                    if meta:
                        file_path = meta["stored_path"]
            if not file_path and att.data:
                import tempfile
                ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir="data/uploads")
                tmp.write(att.data)
                tmp.close()
                file_path = tmp.name

            if file_path:
                import os
                abs_path = os.path.abspath(file_path)

                # Картинки -> отдельно для vision
                is_image = (att.mime_type or "").startswith("image/") or att.type == "image"
                if is_image:
                    mime = att.mime_type or "image/jpeg"
                    raw = open(abs_path, "rb").read()
                    images.append({"data": raw, "mime_type": mime, "filename": fname, "path": abs_path})
                    parts.append(f"[Image: {fname} | path: {abs_path}]")
                    continue

                extracted = await extract_text(file_path, att.mime_type)
                if extracted:
                    parts.append(f"[File: {fname} | path: {abs_path}]\n{extracted}\n[/File]")
                    if self.file_store and att.url:
                        url_parts = (att.url or "").split("/")
                        if len(url_parts) >= 4:
                            await self.file_store.set_text_content(url_parts[3], extracted[:5000])
                else:
                    parts.append(f"[File: {fname} | path: {abs_path} | type: {att.mime_type or '?'}]")
            else:
                parts.append(f"[File: {fname} ({att.mime_type or '?'}) -- file unavailable]")

        text_ctx = "\n\n".join(parts) if parts else None
        return text_ctx, images

    async def _handle_command(self, user_id, text, channel):
        cmd = text.split()[0].lower()
        if cmd == "/new":
            if not self.conversations:
                return Response(text="Чаты не настроены.")
            await self.conversations.create_chat(user_id, channel)
            return Response(text="Новый чат создан. Что хотите обсудить?")
        if cmd == "/history":
            if not self.conversations:
                return Response(text="Чаты не настроены.")
            chats = await self.conversations.list_chats(user_id, limit=10)
            if not chats:
                return Response(text="История пуста.")
            lines = []
            for i, c in enumerate(chats, 1):
                active = " <- текущий" if c.is_active else ""
                lines.append(f"{i}. {c.title} ({c.message_count} сообщ., {c.updated_at[:10]}){active}")
            return Response(text="Последние чаты:\n" + "\n".join(lines))
        if cmd == "/status":
            if not self.conversations:
                return Response(text="Чаты не настроены.")
            chat = await self.conversations.get_or_create_active_chat(user_id)
            count = await self.conversations.count_active_messages(chat.id)
            sflag = "есть" if chat.summary else "нет"
            return Response(text=f"Чат: {chat.title}\nСообщений: {count} (резюме: {sflag})\nСоздан: {chat.created_at[:16]}")
        return None

    async def _run_agent_loop(self, user_id, system, messages, tools, chat_id=None, max_iterations=5):
        for i in range(max_iterations):
            llm_response = await self.llm.complete(messages=messages, system=system,
                                                    tools=tools if tools else None)
            if not llm_response.tool_calls:
                return llm_response.text or "Не могу сформулировать ответ."

            assistant_content = []
            if llm_response.text:
                assistant_content.append({"type": "text", "text": llm_response.text})
            for tc in llm_response.tool_calls:
                tname = f"{tc.skill_name}.{tc.action}" if tc.action != "execute" else tc.skill_name
                assistant_content.append({"type": "tool_use", "id": tc.id, "name": tname, "input": tc.params})
            messages.append(LLMMessage(role="assistant", content=assistant_content))
            if self.conversations and chat_id:
                await self.conversations.add_message(chat_id, "assistant", assistant_content)

            tool_results = []
            for tc in llm_response.tool_calls:
                result = await self.skills.execute_tool(tc.skill_name, tc.action, tc.params)
                tool_results.append({"type": "tool_result", "tool_use_id": tc.id, "content": result})
            messages.append(LLMMessage(role="user", content=tool_results))
            if self.conversations and chat_id:
                await self.conversations.add_message(chat_id, "user", tool_results)
            logger.debug("agent_loop_iteration", iteration=i + 1, tool_calls=len(llm_response.tool_calls))

        return "Достигнут лимит итераций. Попробуйте упростить запрос."

    async def _summarize_for_compaction(self, text):
        prompt = COMPACTION_PROMPT.format(text=text)
        response = await self.llm.complete(messages=[LLMMessage(role="user", content=prompt)], temperature=0.3)
        return response.text or "Резюме недоступно."

    async def _generate_title(self, user_msg, asst_msg):
        u = user_msg[:200] if isinstance(user_msg, str) else str(user_msg)[:200]
        a = asst_msg[:200] if isinstance(asst_msg, str) else str(asst_msg)[:200]
        prompt = TITLE_PROMPT.format(user_msg=u, asst_msg=a)
        response = await self.llm.complete(messages=[LLMMessage(role="user", content=prompt)], temperature=0.5)
        return (response.text or "Чат").strip()[:80]

    async def _handle_legacy(self, message):
        user_id = message.user_id
        memory_context = await self.memory.build_context(user_id)
        tools = self.skills.get_all_tools()
        skills_desc = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.skills.values())
        system = SYSTEM_PROMPT.format(
            memory_context=memory_context or "Пока ничего не известно.",
            summary_context="", skills_context=skills_desc or "Навыки не загружены.",
        )
        history = [LLMMessage(role="user", content=message.text or "")]
        return Response(text=await self._run_agent_loop(user_id, system, history, tools))

    async def handle_event(self, event):
        logger.info("agent_event", type=event.type, source=event.source, title=event.title)
        prompt = f"Произошло событие:\nТип: {event.type.value}\nИсточник: {event.source}\nЗаголовок: {event.title}\nОписание: {event.description}\n\nЕсли важно - кратко уведоми. Если нет - ответь SKIP."
        llm_response = await self.llm.complete(messages=[LLMMessage(role="user", content=prompt)])
        if llm_response.text and "SKIP" not in llm_response.text.upper():
            return Response(text=llm_response.text)
        return None
