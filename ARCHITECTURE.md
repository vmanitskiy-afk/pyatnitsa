# Пятница.ai — Архитектура

## Обзор

Пятница.ai — AI-агент платформа для российского бизнеса. Персональный ассистент, доступный через MAX мессенджер, с интеграциями в 1С, Битрикс24, EasyRedmine и другие российские системы.

## Принципы

1. **Channel-agnostic** — ядро не знает про конкретный мессенджер
2. **Skills-first** — вся функциональность через навыки (SKILL.md)
3. **Memory-persistent** — контекст сохраняется между сессиями
4. **Proactive** — heartbeat позволяет агенту действовать без запроса
5. **Secure-by-default** — изоляция данных, аудит, минимальные права

## Компоненты

```
pyatnitsa/
├── core/                  # Ядро агента
│   ├── agent.py           # Главный класс агента (оркестратор)
│   ├── llm.py             # Абстракция над LLM (GigaChat primary, Claude fallback)
│   ├── router.py          # Роутинг сообщений к навыкам
│   └── context.py         # Контекст разговора + инъекция памяти
│
├── channels/              # Каналы связи (мессенджеры)
│   ├── base.py            # Абстрактный Channel
│   ├── max_channel.py     # MAX мессенджер
│   └── telegram.py        # Telegram (fallback)
│
├── skills/                # Система навыков
│   ├── loader.py          # Загрузчик SKILL.md + Python skills
│   ├── base.py            # Базовый класс Skill
│   └── examples/
│       └── redmine/
│           ├── SKILL.md   # Описание навыка для LLM
│           └── skill.py   # Реализация
│
├── memory/                # Система памяти
│   ├── store.py           # Интерфейс хранения
│   ├── sqlite_store.py    # SQLite реализация
│   └── models.py          # Модели: Fact, Conversation, Summary
│
├── scheduler/             # Heartbeat и cron
│   ├── heartbeat.py       # Периодические проверки
│   └── jobs.py            # Определение задач
│
├── integrations/          # Коннекторы к внешним системам
│   ├── base.py            # Абстрактная интеграция
│   ├── redmine.py         # EasyRedmine API + Playwright
│   ├── bitrix.py          # Битрикс24 REST API
│   └── onec.py            # 1С OData/REST
│
├── api/                   # HTTP API (админка, webhooks)
│   └── server.py          # FastAPI сервер
│
├── config/                # Конфигурация
│   └── settings.py        # Pydantic Settings
│
├── main.py                # Точка входа
├── pyproject.toml         # Зависимости
├── Dockerfile             # Контейнеризация
└── .env.example           # Шаблон переменных окружения
```

## Поток данных

```
Пользователь (MAX/Telegram)
        │
        ▼
   Channel (получает сообщение, нормализует в Message)
        │
        ▼
   Agent.handle_message(message)
        │
        ├── Memory.get_context(user_id)     ← загружает память
        ├── Router.match_skills(message)     ← определяет навыки
        ├── Context.build(message, memory, skills)  ← строит промпт
        ├── LLM.complete(context)            ← вызывает модель
        │       │
        │       ├── tool_call: skill.execute()  ← вызов навыка
        │       └── text: ответ пользователю
        │
        ├── Memory.save(conversation)        ← сохраняет память
        └── Channel.send(response)           ← отправляет ответ
```

## Heartbeat (проактивные действия)

```
Scheduler (каждые N минут)
        │
        ▼
   Heartbeat.tick()
        │
        ├── Проверяет дедлайны в Redmine
        ├── Проверяет новые сообщения в почте
        ├── Проверяет статусы сделок в Битрикс
        │
        ▼
   Если есть что сообщить → Agent.proactive_message(user_id, event)
        │
        ▼
   Channel.send(notification)
```

## Skills-система

Каждый навык — папка с `SKILL.md` (описание для LLM) и `skill.py` (реализация).

### SKILL.md (читается LLM)
```markdown
# Redmine
Навык для работы с EasyRedmine.
## Команды
- создать_проект: Создаёт проект из шаблона trade_v2
- мои_задачи: Показывает задачи текущего пользователя
- статус_проекта: Краткий отчёт по проекту
## Параметры
- project_name: Название проекта
- template: Шаблон (по умолчанию trade_v2)
```

### skill.py (исполняется агентом)
```python
class RedmineSkill(BaseSkill):
    name = "redmine"
    
    async def execute(self, action: str, params: dict) -> str:
        match action:
            case "создать_проект":
                return await self.create_project(params)
            case "мои_задачи":
                return await self.get_my_tasks(params)
```

## Память

Три уровня:
1. **Short-term** — текущий разговор (in-memory)
2. **Long-term facts** — факты о пользователе (SQLite)
3. **Conversation summaries** — сжатые итоги прошлых разговоров

```sql
-- Факты
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,        -- "role", "team", "preferences.timezone"
    value TEXT NOT NULL,
    source TEXT,              -- откуда узнали
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Разговоры
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    messages JSON NOT NULL,
    summary TEXT,
    created_at TIMESTAMP
);
```

## LLM абстракция

```python
class LLMProvider(ABC):
    async def complete(self, messages, tools) -> LLMResponse: ...

class GigaChatProvider(LLMProvider):  # primary (GigaChat-2-Max)
class ClaudeProvider(LLMProvider):    # optional fallback
```

Model failover: GigaChat → Claude (если подключён)

## Безопасность

- Все секреты в .env, не в коде
- SKILL.md — read-only для LLM (описание), не исполняется
- skill.py — исполняется в sandbox с ограниченными правами
- Каждый клиент = изолированный workspace (multi-tenant в будущем)
- Аудит-лог всех действий агента
