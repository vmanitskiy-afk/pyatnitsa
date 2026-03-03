#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Пятница.ai — Скрипт установки на VPS
# Запуск: curl -sSL ... | bash  или  bash deploy.sh
# ═══════════════════════════════════════════════════════════

set -e

APP_DIR="/opt/pyatnitsa"
REPO_DIR="$APP_DIR"

echo "
╔═══════════════════════════════════════╗
║         🤖 Пятница.ai Setup          ║
║   AI-агент для российского бизнеса    ║
╚═══════════════════════════════════════╝
"

# ─── 1. Проверка системы ─────────────────────────────────────
echo "📋 Проверка системы..."

if [ "$EUID" -ne 0 ]; then
    echo "❌ Запустите скрипт от root: sudo bash deploy.sh"
    exit 1
fi

# ─── 2. Установка Docker ────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "🐳 Устанавливаю Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    echo "✅ Docker установлен"
else
    echo "✅ Docker уже установлен"
fi

# ─── 3. Создание директорий ─────────────────────────────────
echo "📁 Создаю директории..."
mkdir -p $APP_DIR/{data,skills,logs}

# ─── 4. Копирование проекта ─────────────────────────────────
echo "📦 Копирую проект..."
# Если проект загружен вручную — он уже в $APP_DIR
# Если через git:
# git clone https://github.com/your-repo/pyatnitsa.git $APP_DIR

# ─── 5. Создание .env ───────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "⚙️  Настройка конфигурации..."
    echo ""
    
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    
    # GigaChat credentials
    echo "📝 Для GigaChat нужны credentials из https://developers.sber.ru/studio"
    echo "   Создайте проект → Client ID + Client Secret"
    echo "   Закодируйте: echo -n 'client_id:client_secret' | base64"
    echo ""
    read -p "🔑 GigaChat credentials (base64): " GIGACHAT_CREDS
    sed -i "s|LLM__GIGACHAT_CREDENTIALS=.*|LLM__GIGACHAT_CREDENTIALS=$GIGACHAT_CREDS|" "$APP_DIR/.env"
    
    # GigaChat scope
    echo ""
    echo "   Scope: GIGACHAT_API_PERS (физлицо) / GIGACHAT_API_B2B (бизнес)"
    read -p "🏢 GigaChat scope [GIGACHAT_API_PERS]: " GC_SCOPE
    GC_SCOPE=${GC_SCOPE:-GIGACHAT_API_PERS}
    sed -i "s|LLM__GIGACHAT_SCOPE=.*|LLM__GIGACHAT_SCOPE=$GC_SCOPE|" "$APP_DIR/.env"
    
    # MAX Bot Token
    read -p "💬 MAX Bot Token (от @MasterBot): " MAX_TOKEN
    sed -i "s|CHANNELS__MAX_BOT_TOKEN=.*|CHANNELS__MAX_BOT_TOKEN=$MAX_TOKEN|" "$APP_DIR/.env"
    
    # Redmine (опционально)
    read -p "📊 Redmine URL (или Enter для пропуска): " REDMINE_URL
    if [ -n "$REDMINE_URL" ]; then
        sed -i "s|INTEGRATIONS__REDMINE_URL=.*|INTEGRATIONS__REDMINE_URL=$REDMINE_URL|" "$APP_DIR/.env"
        read -p "📊 Redmine API Key: " REDMINE_KEY
        sed -i "s|INTEGRATIONS__REDMINE_API_KEY=.*|INTEGRATIONS__REDMINE_API_KEY=$REDMINE_KEY|" "$APP_DIR/.env"
    fi
    
    echo "✅ .env создан"
else
    echo "✅ .env уже существует"
fi

# ─── 6. Запуск ──────────────────────────────────────────────
echo ""
echo "🚀 Запускаю Пятница.ai..."
cd $APP_DIR
docker compose up -d --build

echo ""
echo "═══════════════════════════════════════════"
echo "✅ Пятница.ai запущена!"
echo ""
echo "📋 Полезные команды:"
echo "   docker compose logs -f        — логи"
echo "   docker compose restart        — перезапуск"
echo "   docker compose down           — остановка"
echo "   curl localhost:8080/health    — проверка"
echo ""
echo "💬 Откройте MAX мессенджер и напишите боту!"
echo "═══════════════════════════════════════════"
