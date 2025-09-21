import yt_dlp
import asyncio
import tempfile
import subprocess
import shutil
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from app.config import settings
from app.services.job_manager import job_manager
from app.models import VideoFormat

# Remove ANSI color codes
ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


class VideoDownloader:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def _get_base_options(self) -> Dict[str, Any]:
        """Base yt-dlp options"""
        return {
            "quiet": True,
            "no_color": True,
            "noplaylist": True,
            "http_headers": {
                "User-Agent": self.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
        }

    def _get_cookie_options(self) -> List[Dict[str, Any]]:
        """Get cookie options to try"""
        options = []

        # Try cookies file if exists
        cookies_file = settings.cookies_dir / "cookies.txt"
        if cookies_file.exists():
            options.append(
                {"cookiesfrombrowser": None, "cookiefile": str(cookies_file)}
            )

        # Try browser cookies
        for browser in ["chrome", "firefox", "edge"]:
            options.append({"cookiesfrombrowser": (browser,)})

        # No cookies
        options.append({})

        return options

    def _create_progress_hook(self, job_id: str, loop: asyncio.AbstractEventLoop):
        """Create progress hook for yt-dlp"""

        def hook(d):
            if d["status"] == "downloading":
                percent = d.get("_percent_str", "")
                speed = d.get("_speed_str", "")
                eta = d.get("_eta_str", "")

                # Clean ANSI codes
                percent = ANSI_ESCAPE.sub("", percent).strip()
                speed = ANSI_ESCAPE.sub("", speed).strip()
                eta = ANSI_ESCAPE.sub("", eta).strip()

                asyncio.run_coroutine_threadsafe(
                    job_manager.update_job(
                        job_id,
                        status="downloading",
                        percent=percent,
                        speed=speed,
                        eta=eta,
                        downloaded_bytes=d.get("downloaded_bytes"),
                        total_bytes=d.get("total_bytes")
                        or d.get("total_bytes_estimate"),
                    ),
                    loop,
                )
            elif d["status"] == "finished":
                asyncio.run_coroutine_threadsafe(
                    job_manager.update_job(
                        job_id, status="postprocessing", percent="100%"
                    ),
                    loop,
                )

        return hook

    async def probe_formats(self, url: str) -> Dict[str, Any]:
        """Extract available formats for a video"""
        loop = asyncio.get_event_loop()

        def _extract():
            last_error = None
            for cookie_opt in self._get_cookie_options():
                opts = self._get_base_options()
                opts.update(cookie_opt)

                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(url, download=False)
                except Exception as e:
                    last_error = e
                    continue

            if last_error:
                raise last_error
            raise Exception("Failed to extract video information")

        info = await loop.run_in_executor(None, _extract)

        # Process formats
        formats = []
        for f in info.get("formats", []):
            if not f.get("format_id"):
                continue

            vcodec = f.get("vcodec")
            acodec = f.get("acodec")
            has_video = vcodec and vcodec != "none"
            has_audio = acodec and acodec != "none"

            # Determine type
            if has_video and has_audio:
                fmt_type = "av"
            elif has_video:
                fmt_type = "video"
            elif has_audio:
                fmt_type = "audio"
            else:
                fmt_type = "other"

            # Skip non-video formats for simplicity
            if fmt_type not in ("av", "video"):
                continue

            # Build format string (add audio for video-only)
            if fmt_type == "video":
                fmt_str = f"{f['format_id']}+bestaudio[ext=m4a]/bestaudio"
            else:
                fmt_str = f["format_id"]

            # Безопасное получение числовых значений
            fps_value = f.get("fps")
            if fps_value is not None:
                fps_value = (
                    int(fps_value) if isinstance(fps_value, (int, float)) else None
                )

            tbr_value = f.get("tbr")
            if tbr_value is not None:
                tbr_value = (
                    round(tbr_value) if isinstance(tbr_value, (int, float)) else None
                )

            formats.append(
                VideoFormat(
                    id=f["format_id"],
                    type=fmt_type,
                    label=self._build_format_label(f),
                    ext=f.get("ext"),
                    res=(
                        f"{f.get('width')}x{f.get('height')}"
                        if f.get("width") and f.get("height")
                        else None
                    ),
                    fps=fps_value,
                    height=f.get("height"),
                    tbr=tbr_value,
                    vcodec=vcodec,
                    acodec=acodec,
                    fmt=fmt_str,
                )
            )

        # Sort formats
        formats.sort(
            key=lambda x: (0 if x.type == "av" else 1, -(x.height or 0), -(x.tbr or 0))
        )

        return {
            "meta": {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
            },
            "formats": [f.dict() for f in formats],
        }

    def _build_format_label(self, f: Dict) -> str:
        """Build human-readable format label"""
        parts = []

        # Type
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        if vcodec and vcodec != "none" and acodec and acodec != "none":
            parts.append("AV")
        elif vcodec and vcodec != "none":
            parts.append("VIDEO")
        elif acodec and acodec != "none":
            parts.append("AUDIO")

        # Resolution
        if f.get("height"):
            parts.append(f"{f['height']}p")

        # FPS
        if f.get("fps"):
            fps_val = int(f["fps"]) if isinstance(f["fps"], (int, float)) else f["fps"]
            parts.append(f"{fps_val}fps")

        # Extension
        if f.get("ext"):
            parts.append(f["ext"].upper())

        # Bitrate
        if f.get("tbr"):
            tbr_val = (
                round(f["tbr"]) if isinstance(f["tbr"], (int, float)) else f["tbr"]
            )
            parts.append(f"{tbr_val}k")

        # File size
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            size_str = self._format_size(size)
            parts.append(f"~{size_str}")

        return " • ".join(parts)

    def _format_size(self, bytes_val: int) -> str:
        """Format bytes to human readable"""
        if not bytes_val:
            return ""
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(bytes_val)
        unit_index = 0
        while value >= 1024 and unit_index < len(units) - 1:
            value /= 1024
            unit_index += 1
        return f"{value:.1f} {units[unit_index]}"

    async def download(self, job_id: str, url: str, fmt: str):
        """Download video with given format"""
        async with self.semaphore:
            await self._download_internal(job_id, url, fmt)

    async def _download_internal(self, job_id: str, url: str, fmt: str):
        """Internal download implementation"""
        loop = asyncio.get_event_loop()
        temp_dir = None

        try:
            # Create temp directory
            temp_dir = Path(tempfile.mkdtemp(prefix="video_", dir=settings.temp_dir))

            # Update status
            await job_manager.update_job(job_id, status="starting")

            # Download
            def _download():
                last_error = None
                for cookie_opt in self._get_cookie_options():
                    opts = self._get_base_options()
                    opts.update(
                        {
                            "format": fmt,
                            "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
                            "progress_hooks": [
                                self._create_progress_hook(job_id, loop)
                            ],
                            "merge_output_format": "mp4",
                        }
                    )
                    opts.update(cookie_opt)

                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                            return self._resolve_output_path(ydl, info, temp_dir)
                    except Exception as e:
                        last_error = e
                        continue

                if last_error:
                    raise last_error
                raise Exception("Download failed")

            filepath = await loop.run_in_executor(None, _download)

            # Post-process if needed
            await job_manager.update_job(job_id, status="postprocessing")
            filepath = await self._ensure_compatible_format(filepath)

            # Check file size
            file_size = filepath.stat().st_size
            if file_size > settings.max_file_size:
                raise Exception(f"File too large: {file_size / 1024 / 1024:.1f}MB")

            # Update job as finished
            await job_manager.update_job(
                job_id,
                status="finished",
                filepath=str(filepath),
                filename=filepath.name,
                percent="100%",
            )

        except Exception as e:
            # Cleanup on error
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

            error_msg = str(e)
            await job_manager.update_job(job_id, status="error", error=error_msg)
            raise

    def _resolve_output_path(
        self, ydl: yt_dlp.YoutubeDL, info: Dict, temp_dir: Path
    ) -> Path:
        """Find the actual output file"""
        filename = ydl.prepare_filename(info)
        base = Path(filename).stem

        # Try common extensions
        for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3"):
            candidate = temp_dir / f"{base}{ext}"
            if candidate.exists():
                return candidate

        # Fallback - find any file in temp dir
        files = list(temp_dir.glob("*"))
        if files:
            return files[0]

        raise Exception("Output file not found")

    async def _ensure_compatible_format(self, filepath: Path) -> Path:
        """Ensure video is in a compatible format"""
        if not shutil.which("ffmpeg"):
            return filepath

        # Only process MP4 files
        if filepath.suffix.lower() != ".mp4":
            return filepath

        loop = asyncio.get_event_loop()

        def _process():
            # Create temp file
            temp_file = filepath.with_suffix(".temp.mp4")

            try:
                # Remux with faststart
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(filepath),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    "-loglevel",
                    "error",
                    str(temp_file),
                ]

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300
                )

                if result.returncode == 0 and temp_file.exists():
                    # Replace original with processed file
                    filepath.unlink()
                    temp_file.rename(filepath)
            except:
                # Cleanup temp file on error
                if temp_file.exists():
                    temp_file.unlink()

        await loop.run_in_executor(None, _process)
        return filepath


# Global instance
downloader = VideoDownloader()
