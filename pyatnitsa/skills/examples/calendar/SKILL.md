# Навык Calendar (Mail.ru CalDAV)

Управление календарём Mail.ru через CalDAV + SMTP-приглашения.

## Инструменты

| Инструмент | Описание |
|---|---|
| `calendar.list` | Список событий за N дней (по умолчанию 7) |
| `calendar.create` | Создать событие (title, start, end, description, location, attendees) |
| `calendar.invite` | Создать событие + отправить email-приглашения (ICS + SMTP) |
| `calendar.update` | Обновить событие по UID |
| `calendar.delete` | Удалить событие по UID |

## Формат дат

ISO datetime: `2025-03-15T10:00` (часовой пояс — MAILRU_TIMEZONE).

## Настройка

- `MAILRU_USER` — email (user@mail.ru)
- `MAILRU_APP_PASSWORD` — пароль приложения Mail.ru
- `MAILRU_CALDAV_URL` — URL CalDAV календаря
- `MAILRU_TIMEZONE` — часовой пояс (по умолчанию Europe/Moscow)
