# Навык RusProfile

Поиск информации о компаниях на rusprofile.ru через Playwright (headless scraping).

## Инструменты

| Инструмент | Описание |
|---|---|
| `rusprofile.lookup` | Поиск компании по ИНН, ОГРН или названию |

## Параметры lookup

- `inn` — ИНН (10 цифр юрлицо, 12 цифр ИП)
- `ogrn` — ОГРН (13 или 15 цифр)
- `name` — название компании

Минимум один параметр обязателен. Приоритет: ИНН → ОГРН → название.

## Что возвращает

- `full_name`, `short_name` — полное и сокращённое наименование
- `inn`, `kpp`, `ogrn` — реквизиты
- `opf_type` — `org` или `ip`
- `status` — Действующее / Ликвидировано
- `registration_date`, `address`
- `okved_main`, `okved_extra[]` — ОКВЭД
- `manager` — { position, name }
- `contacts` — { phone[], website }
- `rusprofile_url` — ссылка на карточку

## Зависимости

```bash
pip install playwright
playwright install chromium
```
