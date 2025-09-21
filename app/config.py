from pydantic_settings import BaseSettings
from typing import List, Optional
from pathlib import Path
import os

class Settings(BaseSettings):
    app_name: str = "Video Downloader"
    debug: bool = False
    
    # Paths - используем переменные окружения из docker-compose
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "/downloads/temp"))
    cookies_dir: Path = Path(os.getenv("COOKIES_DIR", "/app/cookies"))
    
    # Limits
    max_file_size: int = int(os.getenv("MAX_FILE_SIZE", str(5 * 1024 * 1024 * 1024)))  # 5GB default
    max_concurrent_downloads: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
    job_ttl_hours: int = int(os.getenv("JOB_TTL_HOURS", "24"))
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
    
    # Security
    secret_key: str = os.getenv("SECRET_KEY", "change-this-secret-key")
    
    # Парсим allowed_origins из переменной окружения
    @property
    def allowed_origins(self) -> List[str]:
        origins = os.getenv("ALLOWED_ORIGINS", "https://video.vitalyor.online,http://localhost:8000")
        return [origin.strip() for origin in origins.split(",")]
    
    # Поддерживаемые домены
    allowed_domains: List[str] = [
        "youtube.com", "youtu.be",
        "instagram.com", "tiktok.com",
        "twitter.com", "x.com",
        "vimeo.com", "dailymotion.com",
        "reddit.com", "v.redd.it",
        "facebook.com", "fb.watch"
    ]
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        env_ignore_empty = True

settings = Settings()

# Создаём необходимые директории
settings.temp_dir.mkdir(parents=True, exist_ok=True)
