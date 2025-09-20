# FastAPI + yt-dlp с прогрессом и выбором качества

Веб-страница с одним полем URL и выбором профиля (Лучшее, 1080p, 720p, Только аудио). 
Скачивание идёт через `yt-dlp`, прогресс отдаётся в реальном времени по WebSocket.
Готовый файл доступен по ссылке, временные файлы удаляются после выдачи.

## 1) Предусловия

- Python 3.10+ (рекомендую 3.12)
- Установленный `ffmpeg` в системе:
  - **macOS (brew):** `brew install ffmpeg`
  - **Ubuntu/Debian:** `sudo apt update && sudo apt install -y ffmpeg`
- Доступ в интернет с сервера

## 2) Локальный запуск (без Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# старт dev-сервера
uvicorn app.main:app --reload