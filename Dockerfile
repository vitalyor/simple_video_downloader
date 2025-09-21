FROM python:3.11-slim

# Установка системных зависимостей
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копирование зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование приложения
COPY app app/
COPY .env .

# Создание пользователя для безопасности
RUN useradd -m -u 1000 appuser && \
    mkdir -p /tmp/video_downloads && \
    chown -R appuser:appuser /app /tmp/video_downloads

USER appuser

# Запуск
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
