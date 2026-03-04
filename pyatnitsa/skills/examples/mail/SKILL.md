# Навык Mail (Mail.ru IMAP/SMTP)

Работа с почтой Mail.ru: чтение входящих, поиск, отправка, ответы, пересылка.

## Инструменты

| Инструмент | Описание |
|---|---|
| `mail.inbox` | Входящие письма (limit, unseen, folder) |
| `mail.read` | Прочитать письмо по UID (полный текст + вложения) |
| `mail.search` | Поиск по теме, отправителю, получателю, дате |
| `mail.send` | Отправить письмо (to, subject, body, cc, html) |
| `mail.reply` | Ответить на письмо по UID (цитирует оригинал) |
| `mail.forward` | Переслать письмо по UID другому адресату |
| `mail.flag` | Пометить письмо: seen, unseen, flagged, unflagged, delete |

## Настройка

- `MAILRU_USER` — email адрес (user@mail.ru)
- `MAILRU_APP_PASSWORD` — пароль приложения (НЕ основной пароль). Создаётся в Mail.ru → Настройки → Безопасность → Пароли для внешних приложений.

## Важные замечания

- UID — уникальный идентификатор письма, получается из `mail.inbox` или `mail.search`
- Для `mail.reply` и `mail.forward` нужен UID: сначала найти письмо через inbox/search, потом ответить
- Папки: INBOX (входящие), Sent (отправленные), Drafts (черновики), Spam, Trash
- IMAP: `imap.mail.ru:993` (SSL), SMTP: `smtp.mail.ru:465` (SSL)
- Если IMAP недоступен — включить в Mail.ru → Настройки → Почтовые программы
