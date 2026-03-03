# 🚀 Деплой Пятница.ai на VPS

## Шаг 1: Получить токен MAX бота

1. Откройте **MAX мессенджер** (приложение или max.im)
2. Найдите **@MasterBot** в поиске
3. Напишите `/start` → нажмите **"Создать бота"**
4. Введите **имя бота**: `Пятница` (или любое)
5. Введите **username бота**: `pyatnitsa_bot` (уникальный, с `_bot` на конце)
6. MasterBot выдаст **токен** — скопируйте его, он выглядит как длинная строка
7. Сохраните токен — он понадобится в следующем шаге

> ⚠️ Если у вас ещё нет аккаунта MAX — скачайте приложение: 
> iOS: App Store, Android: Google Play, Web: max.im

## Шаг 2: Получить GigaChat credentials

1. Откройте https://developers.sber.ru/studio/workspaces
2. Зарегистрируйтесь / войдите (через SberID или по логину)
3. **Создайте проект** → выберите GigaChat API
4. Скопируйте **Client ID** и **Client Secret**
5. Закодируйте в base64:
   ```bash
   echo -n "ваш_client_id:ваш_client_secret" | base64
   ```
6. Результат (длинная строка) — это ваши **credentials**

> 💡 Бесплатный тариф даёт 1 000 000 токенов для GigaChat-2.
> Для бизнеса — выберите scope `GIGACHAT_API_B2B` при настройке.

## Шаг 3: Подключиться к VPS

```bash
ssh root@YOUR_VPS_IP
```

## Шаг 4: Загрузить проект на VPS

**Вариант А — через scp (с локальной машины):**
```bash
# На локальной машине:
scp -r ./pyatnitsa root@YOUR_VPS_IP:/opt/pyatnitsa
```

**Вариант Б — через git (если загрузили в репо):**
```bash
# На VPS:
git clone https://github.com/YOUR_REPO/pyatnitsa.git /opt/pyatnitsa
```

**Вариант В — скачать архив:**
```bash
# На VPS:
mkdir -p /opt/pyatnitsa
cd /opt/pyatnitsa
# загрузить файлы любым способом
```

## Шаг 5: Запуск автоматическим скриптом

```bash
cd /opt/pyatnitsa
chmod +x deploy.sh
sudo bash deploy.sh
```

Скрипт спросит:
- 🔑 GigaChat credentials (base64)
- 🏢 GigaChat scope (PERS/B2B)
- 💬 MAX Bot Token
- 📊 Redmine URL (опционально)

## Шаг 5 (альтернатива): Ручная настройка

```bash
cd /opt/pyatnitsa

# Создать .env из шаблона
cp .env.example .env

# Отредактировать .env
nano .env
# Прописать:
#   LLM__GIGACHAT_CREDENTIALS=ваши_base64_credentials
#   CHANNELS__MAX_BOT_TOKEN=ваш_токен_от_MasterBot

# Создать директории
mkdir -p data skills logs

# Запустить
docker compose up -d --build
```

## Шаг 6: Проверить что всё работает

```bash
# Проверить статус контейнера
docker compose ps

# Проверить логи
docker compose logs -f

# Проверить health endpoint
curl http://localhost:8080/health
# Ожидаемый ответ: {"status":"ok","service":"pyatnitsa","version":"0.1.0"}
```

## Шаг 7: Написать боту в MAX!

1. Откройте MAX мессенджер
2. Найдите своего бота по username (например `@pyatnitsa_bot`)
3. Нажмите **"Начать"**
4. Напишите: **"Привет, Пятница!"**
5. Бот должен ответить 🎉

---

## Полезные команды

```bash
# Логи в реальном времени
docker compose logs -f

# Перезапуск
docker compose restart

# Остановка
docker compose down

# Обновление (после git pull)
docker compose up -d --build

# Зайти внутрь контейнера
docker compose exec pyatnitsa bash

# Посмотреть базу памяти
docker compose exec pyatnitsa python -c "
import sqlite3, json
db = sqlite3.connect('data/memory.db')
for row in db.execute('SELECT * FROM facts'):
    print(row)
"
```

## Траблшутинг

| Проблема | Решение |
|----------|---------|
| `maxapi not installed` | Проверьте что Dockerfile скачивает max-botapi-python |
| `no_llm_providers` | Проверьте LLM__GIGACHAT_CREDENTIALS в .env |
| `no_channels` | Проверьте CHANNELS__MAX_BOT_TOKEN в .env |
| Бот не отвечает | `docker compose logs -f` — смотрите ошибки |
| `401 Unauthorized` от GigaChat | Проверьте credentials и scope на developers.sber.ru |
| SSL ошибки GigaChat | Установите GIGACHAT_VERIFY_SSL=false или скачайте сертификат Минцифры |
| MAX polling не работает | Убедитесь что у бота нет webhook подписок |
