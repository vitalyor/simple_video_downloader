import asyncio
from typing import Dict, Optional, Set
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import os
from app.models import JobStatus
from app.config import settings
from fastapi import WebSocket

class JobManager:
    def __init__(self):
        self.jobs: Dict[str, JobStatus] = {}
        self.websockets: Dict[str, Set[WebSocket]] = {}
        self.queues: Dict[str, asyncio.Queue] = {}
        self.cleanup_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Start background cleanup task"""
        if not self.cleanup_task:
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self):
        """Stop background tasks and cleanup"""
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Cleanup all temp files
        for job_id, job in self.jobs.items():
            if job.filepath and Path(job.filepath).exists():
                try:
                    Path(job.filepath).unlink()
                except:
                    pass
    
    async def _cleanup_loop(self):
        """Periodically cleanup old jobs and files"""
        while True:
            try:
                await asyncio.sleep(3600)  # Every hour
                await self.cleanup_old_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Cleanup error: {e}")
    
    async def cleanup_old_jobs(self):
        """Remove old jobs and their files"""
        now = datetime.now()
        ttl = timedelta(hours=settings.job_ttl_hours)
        
        expired_jobs = []
        for job_id, job in self.jobs.items():
            if now - job.created_at > ttl:
                expired_jobs.append(job_id)
        
        for job_id in expired_jobs:
            await self.remove_job(job_id)
    
    async def create_job(self, job_id: str) -> JobStatus:
        """Create a new job"""
        job = JobStatus(job_id=job_id, status="queued")
        self.jobs[job_id] = job
        self.queues[job_id] = asyncio.Queue()
        return job
    
    async def update_job(self, job_id: str, **kwargs):
        """Update job status and notify websocket clients"""
        if job_id not in self.jobs:
            return
        
        job = self.jobs[job_id]
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = datetime.now()
        
        # Notify websocket clients
        await self.broadcast(job_id, job.dict())
    
    async def get_job(self, job_id: str) -> Optional[JobStatus]:
        """Get job status"""
        return self.jobs.get(job_id)
    
    async def remove_job(self, job_id: str):
        """Remove job and cleanup resources"""
        job = self.jobs.get(job_id)
        if job:
            # Cleanup file
            if job.filepath and Path(job.filepath).exists():
                try:
                    # If file is in temp dir, remove parent directory too
                    filepath = Path(job.filepath)
                    if str(settings.temp_dir) in str(filepath):
                        parent = filepath.parent
                        shutil.rmtree(parent, ignore_errors=True)
                    else:
                        filepath.unlink()
                except:
                    pass
            
            # Remove from tracking
            self.jobs.pop(job_id, None)
            self.queues.pop(job_id, None)
            
            # Close websockets
            for ws in self.websockets.get(job_id, set()).copy():
                try:
                    await ws.close()
                except:
                    pass
            self.websockets.pop(job_id, None)
    
    async def add_websocket(self, job_id: str, websocket: WebSocket):
        """Add websocket connection for job"""
        if job_id not in self.websockets:
            self.websockets[job_id] = set()
        self.websockets[job_id].add(websocket)
    
    async def remove_websocket(self, job_id: str, websocket: WebSocket):
        """Remove websocket connection"""
        if job_id in self.websockets:
            self.websockets[job_id].discard(websocket)
            if not self.websockets[job_id]:
                self.websockets.pop(job_id, None)
    
    async def broadcast(self, job_id: str, data: dict):
        """Broadcast to all websocket clients for a job"""
        dead_sockets = set()
        for ws in self.websockets.get(job_id, set()).copy():
            try:
                await ws.send_json(data)
            except:
                dead_sockets.add(ws)
        
        # Remove dead connections
        for ws in dead_sockets:
            await self.remove_websocket(job_id, ws)

# Global instance
job_manager = JobManager()
