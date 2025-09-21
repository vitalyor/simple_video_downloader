from pydantic_settings import BaseSettings
from typing import List, Optional
from pathlib import Path
import json


class Settings(BaseSettings):
    app_name: str = "Video Downloader"
    debug: bool = False

    # Paths
    temp_dir: Path = Path("/tmp/video_downloads")
    cookies_dir: Path = Path(__file__).parent / "cookies"

    # Limits
    max_file_size: int = 2 * 1024 * 1024 * 1024  # 2GB
    max_concurrent_downloads: int = 3
    job_ttl_hours: int = 24
    rate_limit_per_minute: int = 10

    # Security
    secret_key: str = "change-this-secret-key"
    allowed_origins: List[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    allowed_domains: List[str] = [
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "vimeo.com",
        "dailymotion.com",
    ]

    # Redis (optional)
    redis_url: Optional[str] = None

    class Config:
        env_file = ".env"

        # Парсим JSON для сложных типов
        @classmethod
        def parse_env_var(cls, field_name: str, raw_val: str):
            if field_name in ("allowed_origins", "allowed_domains"):
                # Если строка выглядит как JSON список
                if raw_val.startswith("["):
                    return json.loads(raw_val)
                # Если просто строка с запятыми
                return [x.strip() for x in raw_val.split(",")]
            return raw_val


# Создаём экземпляр настроек
settings = Settings()

# Создаём необходимые директории
settings.temp_dir.mkdir(parents=True, exist_ok=True)
settings.cookies_dir.mkdir(parents=True, exist_ok=True)
