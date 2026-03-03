"""Agент Пятница -- главный оркестратор.

Персистентные чаты, компакция длинных диалогов, команды /new /history.
"""

from __future__ import annotations

from typing import Any

import structlog

from pyatnitsa.core.models import Message, Response, Event, ToolCall
from pyatnitsa.core.llm import LLMManager, LLMMessage, LLMTool
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
                 memory: MemoryStore, conversations: ConversationStore | None = None):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        self.conversations = conversations

    async def handle_message(self, message: Message) -> Response:
        user_id = message.user_id
        text = (message.text or "").strip()
        logger.info("agent_message_received", user_id=user_id,
                     channel=message.channel, text=text[:100])

        if text.startswith("/"):
            cmd_response = await self._handle_command(user_id, text, message.channel)
            if cmd_response:
                return cmd_response

        if not self.conversations:
            return await self._handle_legacy(message)

        conv = self.conversations
        chat = await conv.get_or_create_active_chat(user_id, message.channel)
        await conv.add_message(chat.id, "user", text)

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
        response_text = await self._run_agent_loop(user_id, system, history, tools, chat.id)
        await conv.add_message(chat.id, "assistant", response_text)
        await conv.maybe_set_title(chat.id, self._generate_title)

        if await conv.needs_compaction(chat.id):
            logger.info("compaction_triggered", chat_id=chat.id)
            await conv.compact(chat.id, self._summarize_for_compaction)

        return Response(text=response_text)

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
