FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/uptimebot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The SQLite database lives here; mount a volume to persist it across restarts.
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "app.py"]
