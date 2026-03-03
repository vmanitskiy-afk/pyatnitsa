# Навык Browser

Универсальная Playwright автоматизация: stateless, с сохранением сессии (cookies/localStorage).

## Инструменты

### Навигация
| Инструмент | Описание |
|---|---|
| `browser.navigate` | Открыть URL |
| `browser.nav_shot` | Открыть URL + скриншот |
| `browser.login` | Combo: URL → логин/пароль → submit → скриншот |

### Взаимодействие
| Инструмент | Описание |
|---|---|
| `browser.click` | Кликнуть по CSS-селектору |
| `browser.click_text` | Кликнуть по тексту (ссылка/кнопка) |
| `browser.fill` | Заполнить поле (очистить + ввести) |
| `browser.type` | Напечатать текст (добавить к существующему) |
| `browser.select` | Выбрать значение в `<select>` |
| `browser.scroll` | Прокрутка (up/down/CSS-селектор) |
| `browser.press` | Нажать клавишу (Enter, Tab...) |

### Извлечение данных
| Инструмент | Описание |
|---|---|
| `browser.screenshot` | Скриншот (вся страница или элемент) |
| `browser.extract` | Текст элементов по селектору (до 50) |
| `browser.html` | HTML элемента или страницы |
| `browser.links` | Все ссылки (до 100) |
| `browser.inputs` | Все input/textarea/select/button |
| `browser.eval` | Выполнить JavaScript |

## Настройка

- `BROWSER_DATA_DIR` — каталог для хранения состояния (по умолчанию ~/.pyatnitsa)

## Зависимости

```bash
pip install playwright
playwright install chromium
```
