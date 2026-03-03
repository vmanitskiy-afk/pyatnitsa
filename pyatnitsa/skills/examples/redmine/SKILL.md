# Навык Redmine (EasyRedmine)

Полная интеграция с EasyRedmine для управления проектами, задачами, участниками и сделками.

## Инструменты

### Задачи
| Инструмент | Описание |
|---|---|
| `redmine.my_tasks` | Мои задачи (status, limit) |
| `redmine.list_issues` | Список задач проекта (project, assigned_to, status, tracker, priority) |
| `redmine.get_issue` | Детали задачи по ID (история, дети) |
| `redmine.create_task` | Создание задачи (project, subject, description, assigned_to, tracker, priority, due_date) |
| `redmine.update_task` | Обновление задачи (id, status, priority, assigned_to, done_ratio, notes) |
| `redmine.comment` | Комментарий к задаче (id, text) |
| `redmine.log_time` | Списание времени (id, hours, activity, comment, date) |

### Проекты
| Инструмент | Описание |
|---|---|
| `redmine.project_status` | Сводка по проекту (открытые задачи, приоритеты, исполнители) |
| `redmine.list_projects` | Список всех проектов |
| `redmine.members` | Участники проекта с ролями |

### Пользователи и контрагенты
| Инструмент | Описание |
|---|---|
| `redmine.me` | Текущий пользователь API-ключа |
| `redmine.find_user` | Поиск по ФИО/фамилии (fuzzy) |
| `redmine.find_counterparty` | Поиск контрагента по названию или ИНН |

### Учёт времени
| Инструмент | Описание |
|---|---|
| `redmine.time_entries` | Записи учёта времени (project, user, from/to) |

### Сделки (бизнес-процесс)
| Инструмент | Описание |
|---|---|
| `redmine.create_deal_project` | **Создание проекта сделки** — 5-фазная архитектура |
| `redmine.create_from_template` | Создание проекта из шаблона EasyRedmine (Playwright) |

## Создание проекта сделки

`create_deal_project` выполняет 5 фаз:

1. **Phase 0: Pre-flight** — разрешение всех lookup-полей (контрагент, АП, РП, менеджер) с паттерном resolveChoice (HARD STOP при неоднозначности)
2. **Phase 1** — создание проекта (API)
3. **Phase 2** — назначение ролей (Ответственный, Исполнитель, Диспетчер, Контролёр) с merge существующих
4. **Phase 3** — установка кастомных полей проекта (cf_241-247)
5. **Phase 4** — создание задачи «Паспорт проекта» (tracker 41) + watchers (РП, Менеджер)
6. **Phase 5** — создание задачи «Расчёт» (tracker 28) + watchers (РП, Пресейл, фиксированные)

### Обязательные параметры:
- `name` — название сделки
- `description` — описание
- `counterparty` — контрагент (название или ИНН)

### Опциональные:
- `ap`, `rp`, `manager` — ФИО (fuzzy поиск по участникам)
- `counterparty_id`, `ap_id`, `rp_id`, `manager_id` — прямые ID
- `stage` — стадия (по умолчанию: Проектирование (расчёт))
- `no_calculation` — пропустить создание задачи Расчёт

## Настройка

Переменные окружения:
- `REDMINE_URL` — адрес EasyRedmine (https://rdm.example.com)
- `REDMINE_API_KEY` — API-ключ пользователя
- `REDMINE_ADMIN_KEY` — API-ключ администратора (опционально, для fallback)
- `RDM_LOGIN` — логин для Playwright (браузерная автоматизация)
- `RDM_PASSWORD` — пароль для Playwright

### Playwright (шаблоны)

EasyRedmine защищает создание проектов из шаблонов через CSRF — обычный API не работает.
Навык использует Playwright (headless Chromium) для:
1. Входа в EasyRedmine
2. Открытия `/templates/{ident}/create`
3. Заполнения формы (имя, идентификатор)
4. Отправки и извлечения ID нового проекта

Если Playwright недоступен (не установлен или нет логина/пароля), `create_deal_project` автоматически fallback'ится на прямое API-создание (без шаблона).

Установка Playwright:
```bash
pip install playwright
playwright install chromium
```
