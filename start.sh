#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Пятница.ai — Быстрый запуск без Docker
# Для разработки или лёгкого деплоя на VPS
# ═══════════════════════════════════════════════════════════

set -e

echo "
╔═══════════════════════════════════════╗
║     🤖 Пятница.ai — Quick Start      ║
╚═══════════════════════════════════════╝
"

# Проверка Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3.11+ не найден. Установите: apt install python3 python3-pip python3-venv"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "🐍 Python $PY_VERSION"

# Создание venv
if [ ! -d ".venv" ]; then
    echo "📦 Создаю виртуальное окружение..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Установка зависимостей
echo "📦 Устанавливаю зависимости..."
pip install -q --upgrade pip
pip install -q .
pip install -q git+https://github.com/max-messenger/max-botapi-python.git

# Создание .env если нет
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  Отредактируйте .env перед запуском:"
    echo "   nano .env"
    echo ""
    echo "Минимально нужно:"
    echo "   LLM__CLAUDE_API_KEY=sk-ant-..."
    echo "   CHANNELS__MAX_BOT_TOKEN=ваш_токен"
    echo ""
    exit 0
fi

# Создание директорий
mkdir -p data skills logs

# Запуск
echo ""
echo "🚀 Запускаю Пятница.ai..."
echo ""
python -m pyatnitsa.main
