from fastapi import (
    FastAPI,
    Request,
    Form,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import asyncio
import uuid
import os
from contextlib import asynccontextmanager

from app.config import settings
from app.models import DownloadRequest, ProbeResponse
from app.services.job_manager import job_manager
from app.services.downloader import downloader

# Templates
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await job_manager.start()
    yield
    # Shutdown
    await job_manager.stop()


# Create app
app = FastAPI(
    title=settings.app_name,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
from typing import Dict
from datetime import datetime, timedelta

rate_limit_store: Dict[str, list] = {}


def check_rate_limit(client_ip: str) -> bool:
    """Simple rate limiting"""
    now = datetime.now()
    minute_ago = now - timedelta(minutes=1)

    if client_ip not in rate_limit_store:
        rate_limit_store[client_ip] = []

    # Clean old entries
    rate_limit_store[client_ip] = [
        t for t in rate_limit_store[client_ip] if t > minute_ago
    ]

    # Check limit
    if len(rate_limit_store[client_ip]) >= settings.rate_limit_per_minute:
        return False

    rate_limit_store[client_ip].append(now)
    return True


# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/probe")
async def probe_formats(request: Request, url: str = Form(...)):
    """Get available formats for a video"""
    client_ip = request.client.host

    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    try:
        # Validate URL
        req = DownloadRequest(url=url, fmt="best")  # fmt is dummy here

        # Get formats
        result = await downloader.probe_formats(str(req.url))
        return ProbeResponse(**result)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get formats: {str(e)}")


@app.post("/download")
async def start_download(request: Request, url: str = Form(...), fmt: str = Form(...)):
    """Start video download"""
    client_ip = request.client.host

    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    try:
        # Validate request
        req = DownloadRequest(url=url, fmt=fmt)

        # Create job
        job_id = str(uuid.uuid4())
        await job_manager.create_job(job_id)

        # Start download task
        asyncio.create_task(downloader.download(job_id, str(req.url), req.fmt))

        return JSONResponse({"job_id": job_id})

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to start download: {str(e)}"
        )


@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """Get job progress"""
    job = await job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job.dict()


@app.get("/fetch/{job_id}")
async def fetch_file(job_id: str):
    """Download completed file"""
    job = await job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "finished":
        raise HTTPException(status_code=400, detail="File is not ready")

    if not job.filepath or not Path(job.filepath).exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Schedule cleanup after download
    async def cleanup():
        await asyncio.sleep(60)  # Wait 1 minute
        await job_manager.remove_job(job_id)

    asyncio.create_task(cleanup())

    return FileResponse(
        path=job.filepath,
        filename=job.filename or "video.mp4",
        media_type="application/octet-stream",
    )


@app.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    """WebSocket for real-time progress updates"""
    await websocket.accept()
    await job_manager.add_websocket(job_id, websocket)

    try:
        # Send current status
        job = await job_manager.get_job(job_id)
        if job:
            await websocket.send_json(job.dict())

        # Keep connection alive
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    finally:
        await job_manager.remove_websocket(job_id, websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
