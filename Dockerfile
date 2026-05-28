FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

COPY pyproject.toml ./
RUN poetry install --no-root --only main

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY app/ ./app/

RUN mkdir -p /app/data/sessions /app/data/logs \
    && chmod 700 /app/data/sessions

CMD ["python", "-m", "app.main"]
