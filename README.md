# 🤖 Пятница.ai

**AI-агент для российского бизнеса** — managed-платформа с поддержкой GigaChat, интеграциями с 1С/Битрикс24/EasyRedmine и веб-интерфейсом.

> Альтернатива [OpenClaw](https://openclaw.ai) для российского рынка: GigaChat вместо ChatGPT, MAX мессенджер вместо WhatsApp, 152-ФЗ из коробки.

---

## Возможности

- **Веб-интерфейс** — чат с агентом и панель настроек прямо в браузере
- **GigaChat** (Сбер) — основная LLM, модели 2-го поколения (Max/Pro/Lite)
- **Claude** (Anthropic) — опциональный fallback
- **Навыки (Skills)** — модульная система интеграций, расширяется файлами
- **Память** — факты о пользователе + история разговоров в SQLite
- **Мессенджеры** — MAX (VK), Telegram (опционально)
- **Проактивность** — heartbeat-планировщик проверяет внешние системы

## Быстрый старт

```bash
# Клонировать
git clone https://github.com/vmanitskiy-afk/pyatnitsa.git
cd pyatnitsa

# Установить
python3 -m venv .venv && source .venv/bin/activate
pip install .
pip install git+https://github.com/max-messenger/max-botapi-python.git

# Настроить
cp .env.example .env
# Или: запустить без .env и настроить через веб-панель

# Запустить
python -m pyatnitsa.main
```

Откройте **http://localhost:8080** → Настройки → GigaChat credentials → Чат.

## Docker

```bash
docker compose up -d --build
# → http://localhost:8080
```

## Настройка GigaChat

1. Откройте [developers.sber.ru/studio](https://developers.sber.ru/studio/workspaces)
2. Создайте проект → GigaChat API
3. Скопируйте **Client ID** и **Client Secret**
4. Закодируйте:
   ```bash
   echo -n "client_id:client_secret" | base64
   ```
5. Вставьте результат в веб-панель (Настройки → LLM) или в `.env`:
   ```
   LLM__GIGACHAT_CREDENTIALS=ваша_base64_строка
   ```

> Бесплатный лимит: 1 000 000 токенов на GigaChat-2.

## Архитектура

```
Пользователь
    │
    ├── Веб-интерфейс (WebSocket)
    ├── MAX мессенджер
    └── Telegram
         │
    ┌────▼─────┐
    │  Agent   │ ← Оркестратор с tool-use loop
    ├──────────┤
    │  LLM     │ ← GigaChat → Claude (failover)
    ├──────────┤
    │  Skills  │ ← Redmine, Битрикс24, 1С...
    ├──────────┤
    │  Memory  │ ← SQLite (факты + разговоры)
    └──────────┘
```

## Структура проекта

```
pyatnitsa/
├── api/
│   ├── server.py          # FastAPI — WebSocket чат, REST API, настройки
│   └── web/index.html     # Веб-интерфейс (чат + панель настроек)
├── core/
│   ├── agent.py           # Оркестратор (tool-use loop)
│   ├── llm.py             # GigaChat + Claude провайдеры с failover
│   └── models.py          # Pydantic-модели данных
├── config/
│   ├── settings.py        # Pydantic Settings (.env)
│   └── settings_store.py  # Хранилище настроек в SQLite (веб-панель)
├── channels/
│   └── channels.py        # MAX messenger + Telegram
├── skills/
│   ├── skills.py          # Загрузчик навыков
│   └── examples/redmine/  # Пример: интеграция с EasyRedmine
├── memory/
│   └── store.py           # SQLite — факты + разговоры
├── scheduler/
│   └── heartbeat.py       # Проактивные проверки
└── main.py                # Точка входа
```

## Навыки (Skills)

Каждый навык — папка с двумя файлами:

```
skills/my_skill/
├── SKILL.md    # Описание для LLM (что умеет, когда использовать)
└── skill.py    # Python-класс с инструментами
```

Пятница автоматически загружает навыки и передаёт их описания в LLM как tools/functions.

**Готовые навыки:** EasyRedmine  
**В планах:** Битрикс24, 1С, Почта

## API

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/` | GET | Веб-интерфейс |
| `/ws/chat` | WS | Чат через WebSocket |
| `/api/chat` | POST | Чат через REST |
| `/api/settings` | GET/POST | Настройки |
| `/api/status` | GET | Статус системы |
| `/health` | GET | Healthcheck |

## Стек

| Компонент | Технология |
|---|---|
| Backend | Python 3.11+, FastAPI, asyncio |
| LLM | GigaChat SDK, Anthropic SDK |
| База | SQLite (aiosqlite) |
| Мессенджеры | max-botapi-python, aiogram 3 |
| Деплой | Docker, Docker Compose |

## Целевая аудитория

- **Средний бизнес** (50–500 сотрудников) — автоматизация рутины
- **Госучреждения** — импортозамещение, GigaChat, 152-ФЗ
- **IT-компании** — интеграция с Redmine/Битрикс/1С

## Roadmap

- [x] Архитектура и скелет
- [x] GigaChat с function calling
- [x] Веб-интерфейс (чат + настройки)
- [x] Система навыков
- [x] Память (SQLite)
- [ ] Hot-reload настроек без рестарта
- [ ] Навык Битрикс24
- [ ] Навык 1С
- [ ] MAX мессенджер (бот)
- [ ] Telegram канал
- [ ] Аутентификация веб-панели
- [ ] Мультитенантность

## Лицензия

MIT — см. [LICENSE](LICENSE)
