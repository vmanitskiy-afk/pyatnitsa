FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc git curl \
    && rm -rf /var/lib/apt/lists/*

# Copy all source first (needed for pip install .)
COPY . .

# Python deps from pyproject.toml (includes gigachat SDK)
RUN pip install --no-cache-dir --break-system-packages .

# Install MAX bot library
RUN pip install --no-cache-dir --break-system-packages \
    git+https://github.com/max-messenger/max-botapi-python.git

# Optional: Install Anthropic Claude SDK as fallback
# RUN pip install --no-cache-dir --break-system-packages anthropic

# Optional: Playwright for browser automation skills
# RUN pip install --no-cache-dir --break-system-packages playwright \
#     && playwright install chromium --with-deps

# Data & skills directories
RUN mkdir -p /app/data /app/skills /app/logs

EXPOSE 8080

CMD ["python", "-m", "pyatnitsa.main"]
