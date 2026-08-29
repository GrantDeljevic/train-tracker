FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY train_tracker ./train_tracker
COPY config ./config

CMD ["sh", "-c", "exec uvicorn train_tracker.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
