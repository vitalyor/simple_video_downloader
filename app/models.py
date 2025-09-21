from pydantic import BaseModel, HttpUrl, validator
from typing import Optional, Literal, Dict, Any, List, Union
from datetime import datetime
from urllib.parse import urlparse
from app.config import settings

class DownloadRequest(BaseModel):
    url: HttpUrl
    fmt: str
    
    @validator('url')
    def validate_url(cls, v):
        parsed = urlparse(str(v))
        domain = parsed.netloc.lower().replace('www.', '')
        
        # Check if domain is allowed
        if not any(allowed in domain for allowed in settings.allowed_domains):
            raise ValueError(f'Unsupported domain: {domain}')
        return v
    
    @validator('fmt')
    def validate_format(cls, v):
        if not v or len(v) > 500:
            raise ValueError('Invalid format specification')
        # Prevent command injection
        if any(char in v for char in [';', '&', '|', '`', '$', '(', ')', '\n', '\r']):
            raise ValueError('Invalid characters in format')
        return v

class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "starting", "downloading", "postprocessing", "finished", "error", "cancelled"]
    percent: Optional[str] = None
    speed: Optional[str] = None
    eta: Optional[str] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    filename: Optional[str] = None
    filepath: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = None
    updated_at: datetime = None
    
    def __init__(self, **data):
        if 'created_at' not in data or data['created_at'] is None:
            data['created_at'] = datetime.now()
        if 'updated_at' not in data or data['updated_at'] is None:
            data['updated_at'] = datetime.now()
        super().__init__(**data)

class VideoFormat(BaseModel):
    id: str
    type: Literal["av", "video", "audio", "other"]
    label: str
    ext: Optional[str] = None
    res: Optional[str] = None
    fps: Optional[Union[int, float]] = None
    height: Optional[int] = None
    tbr: Optional[Union[int, float]] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    fmt: str
    
    @validator('fps', pre=True)
    def convert_fps(cls, v):
        if v is not None and isinstance(v, (int, float)):
            return int(v)
        return v
    
    @validator('tbr', pre=True)
    def convert_tbr(cls, v):
        if v is not None and isinstance(v, (int, float)):
            return round(v)
        return v

class ProbeResponse(BaseModel):
    meta: Dict[str, Any]
    formats: List[VideoFormat]
