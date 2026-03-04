# Пятница.ai v0.2 — Проектирование

## 1. Админ-панель

### 1.1 Архитектура

Админ-панель — отдельный SPA на том же FastAPI сервере (`/admin/`).
Чат остаётся на `/`. Общая аутентификация через токен/пароль.

```
/admin/              → admin SPA (HTML/JS)
/admin/api/...       → admin REST API
/                    → чат (как сейчас)
/ws/chat             → WebSocket чата
/api/...             → существующие API чата
```

### 1.2 Модули админки

#### A. Управление скиллами
- GET  /admin/api/skills           — список скиллов, статус (вкл/выкл)
- POST /admin/api/skills/{name}/toggle — вкл/выкл
- GET  /admin/api/skills/{name}/config — настройки скилла
- PUT  /admin/api/skills/{name}/config — обновить настройки
- Хранение: settings_store (SQLite), ключ `skill.{name}.enabled`, `skill.{name}.config`

#### B. Управление пользователями
- GET  /admin/api/users           — список пользователей (из memory.db)
- GET  /admin/api/users/{id}      — профиль: каналы, кол-во сообщений, последняя активность
- POST /admin/api/users/{id}/block — блокировка
- POST /admin/api/users/{id}/role  — назначение роли (admin/user)
- Хранение: новая таблица `users` в memory.db

#### C. Настройки LLM
- GET  /admin/api/llm             — текущие провайдеры, модели, статус
- PUT  /admin/api/llm             — обновить ключи/модели/failover
- POST /admin/api/llm/test        — тест-запрос к провайдеру
- Хранение: settings_store + .env (ключи шифруются в БД)

#### D. Логи и история диалогов
- GET  /admin/api/logs            — последние N записей (из structlog)
- GET  /admin/api/conversations   — список диалогов всех пользователей
- GET  /admin/api/conversations/{id} — содержимое диалога
- Источник: conversations.db + файловый лог

#### E. Аналитика
- GET  /admin/api/stats           — сводка за период
  - Кол-во сообщений (по дням/каналам)
  - Кол-во ошибок LLM
  - Среднее время ответа
  - Использование токенов
  - Популярные скиллы
- Хранение: новая таблица `events` в memory.db (event_type, timestamp, metadata)

### 1.3 Аутентификация

Простой подход для v1:
- Переменная `ADMIN_PASSWORD` в .env
- `/admin/api/auth` — возвращает JWT-токен
- Все `/admin/api/*` проверяют Bearer token
- Чат `/` пока без аутентификации (или optional)

### 1.4 Frontend

Один HTML файл `/admin/index.html` (как текущий чат).
Минимальный стек: vanilla JS + CSS. Без фреймворков — легко поддерживать.

Секции:
- Dashboard (статистика)
- Skills (карточки скиллов с тогглами)
- Users (таблица)
- LLM (формы настроек)
- Logs (лог-лента с фильтрами)
- Conversations (список → просмотр)

---

## 2. Скилл работы с файлами

### 2.1 Концепция

Пользователь указывает **рабочую папку** в настройках.
Агент получает доступ к этой папке и может:
- Читать и анализировать файлы
- Создавать новые файлы
- Искать по содержимому
- Делать сводки и отчёты
- Работать с изображениями

### 2.2 Настройка

```
# В .env или через админку
FILES_WORKSPACE=/Users/vlad/Documents/workspace

# Или per-user через settings_store
files.workspace.{user_id} = /path/to/folder
```

Ограничения безопасности:
- Запрет выхода за пределы workspace (path traversal)
- Макс. размер файла для анализа (50MB)
- Whitelist расширений

### 2.3 Инструменты (tools для LLM)

```python
# Навигация
files_list(path="", recursive=False, pattern="*")
    → Список файлов и папок с метаданными (размер, дата, тип)

files_tree(path="", depth=2)
    → Дерево структуры папки

# Чтение
files_read(path, encoding="auto")
    → Текстовое содержимое файла (txt, csv, json, xml, md, py...)

files_read_excel(path, sheet=None, range=None)
    → Данные из xlsx/xls в табличном виде

files_read_pdf(path, pages=None)
    → Извлечённый текст из PDF

files_read_docx(path)
    → Текст из Word-документа

files_read_image(path)
    → base64 изображения для vision LLM

# Анализ
files_stats(path="")
    → Сводка по папке: кол-во файлов по типам, общий размер, последние изменения

files_search(query, path="", file_types=None)
    → Поиск по содержимому файлов (grep-like)

# Запись
files_write(path, content, encoding="utf-8")
    → Создать/перезаписать текстовый файл

files_write_excel(path, data, sheet="Sheet1")
    → Создать xlsx из данных

files_write_csv(path, data, delimiter=",")
    → Создать CSV

files_copy(src, dst)
    → Копирование файла

files_move(src, dst)
    → Перемещение файла

files_mkdir(path)
    → Создание директории
```

### 2.4 Зависимости

```toml
# pyproject.toml — новые
"openpyxl>=3.1.0",       # xlsx чтение/запись
"python-docx>=1.0.0",    # docx чтение
"PyMuPDF>=1.24.0",       # pdf чтение (fitz)
"chardet>=5.0.0",        # автодетект кодировки
```

### 2.5 Безопасность

```python
class FileSkill(BaseSkill):
    def _safe_path(self, user_path: str) -> Path:
        """Резолвит путь внутри workspace, блокирует traversal."""
        workspace = Path(self.config["workspace"]).resolve()
        target = (workspace / user_path).resolve()
        if not str(target).startswith(str(workspace)):
            raise PermissionError(f"Доступ запрещён: {user_path}")
        return target
```

### 2.6 Структура файлов

```
pyatnitsa/skills/examples/files/
├── files.py          # FileSkill — основной класс
├── readers.py        # Читалки для разных форматов
├── writers.py        # Генерация файлов
├── search.py         # Полнотекстовый поиск
└── SKILL.md          # Описание для LLM
```

---

## 3. Общие изменения

### 3.1 Таблица events (аналитика)

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,  -- 'message', 'llm_call', 'skill_call', 'error'
    user_id TEXT,
    channel TEXT,
    metadata TEXT,             -- JSON: {skill, tokens, latency_ms, error...}
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_events_type_ts ON events(event_type, timestamp);
```

### 3.2 Таблица users

```sql
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT,
    role TEXT DEFAULT 'user',     -- 'admin', 'user', 'blocked'
    channels TEXT,                -- JSON: ["max", "telegram"]
    first_seen REAL,
    last_seen REAL,
    message_count INTEGER DEFAULT 0
);
```

### 3.3 EventTracker (middleware)

```python
class EventTracker:
    """Записывает события для аналитики."""
    async def track(self, event_type, user_id=None, channel=None, **meta):
        await self.db.execute(
            "INSERT INTO events (timestamp, event_type, user_id, channel, metadata) VALUES (?,?,?,?,?)",
            (time.time(), event_type, user_id, channel, json.dumps(meta))
        )
```

Встраивается в agent.handle_message (начало/конец), llm.complete (токены/latency), skill.execute.

---

## 4. Порядок реализации

### Фаза 1: Фундамент (1-2 дня)
1. Таблицы events + users в memory.db
2. EventTracker middleware
3. Admin auth (JWT)
4. Admin API каркас

### Фаза 2: Файловый скилл (1-2 дня)
5. FileSkill — навигация + чтение (txt, csv, json)
6. readers.py — xlsx, pdf, docx
7. writers.py — создание файлов
8. search.py — grep по файлам
9. SKILL.md
10. Настройка workspace через settings

### Фаза 3: Админка (2-3 дня)
11. Dashboard со статистикой
12. Skills management UI
13. Users management UI
14. LLM settings UI
15. Logs viewer
16. Conversations viewer

### Фаза 4: Полировка
17. Тесты
18. Документация
19. Docker-образ обновлённый
