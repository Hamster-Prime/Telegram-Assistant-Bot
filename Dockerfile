FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    aiogram aiohttp httpx ddgs aiosqlite \
    pydantic pydantic-settings structlog tenacity tzdata

COPY app ./app

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1

EXPOSE 8080
CMD ["python", "-m", "app.main"]
