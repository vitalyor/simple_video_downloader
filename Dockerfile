FROM python:3.12-slim

# Установка системных зависимостей
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копирование и установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование приложения
COPY app app/

# Создание директорий
RUN mkdir -p /downloads/temp && \
    chmod 777 /downloads/temp

# Пользователь для безопасности
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app /downloads

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
