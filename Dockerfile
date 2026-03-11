FROM python:3.10.19
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV UV_LINK_MODE=copy
ENV UV_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

RUN apt-get update && apt-get install -y --no-install-recommends libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libasound2 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .python-version ./

RUN uv sync --no-dev --no-install-project

RUN uv run playwright install chromium-headless-shell

COPY . .

RUN mkdir -p /app/videoFile
RUN mkdir -p /app/cookiesFile
RUN mkdir -p /app/db

EXPOSE 5409

CMD ["/app/.venv/bin/python", "sau_backend/sau_backend.py"]
