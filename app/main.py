from fastapi import FastAPI, Request, Form, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import tempfile
import asyncio
import shutil
import os
from typing import Dict, Any, List
import yt_dlp
import uuid
import re
import subprocess

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

UA_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

app = FastAPI(title="Simple Video Downloader")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

progress_store: Dict[str, Dict[str, Any]] = {}

# WebSocket subscribers and per-job event queues
ws_clients: Dict[str, set] = {}
job_queues: Dict[str, asyncio.Queue] = {}
broadcaster_tasks: Dict[str, asyncio.Task] = {}

@app.on_event("shutdown")
async def _graceful_shutdown():
    # cancel broadcaster tasks
    for jid, t in list(broadcaster_tasks.items()):
        try:
            t.cancel()
        except Exception:
            pass
    broadcaster_tasks.clear()
    # drop queues and ws clients
    job_queues.clear()
    ws_clients.clear()

async def _broadcast_loop(job_id: str):
    q = job_queues.get(job_id)
    if q is None:
        q = asyncio.Queue()
        job_queues[job_id] = q
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                # if nobody is listening and job is not active, exit
                listeners = len(ws_clients.get(job_id, set()))
                status = (progress_store.get(job_id) or {}).get('status')
                if listeners == 0 and status in (None, 'finished', 'error'):
                    break
                continue

            # send to all subscribers; drop dead ones
            dead = set()
            for ws in ws_clients.get(job_id, set()).copy():
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                ws_clients.get(job_id, set()).discard(ws)

            st = str(payload.get('status', ''))
            if st in ('finished', 'error'):
                break
    except asyncio.CancelledError:
        # shutdown path
        pass
    finally:
        job_queues.pop(job_id, None)
        ws_clients.pop(job_id, None)
        broadcaster_tasks.pop(job_id, None)

# Helper to enqueue events from any thread
def _enqueue_event(job_id: str, payload: Dict[str, Any], loop: asyncio.AbstractEventLoop | None = None):
    q = job_queues.get(job_id)
    if q is None:
        q = asyncio.Queue()
        job_queues[job_id] = q
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is not None:
        loop.call_soon_threadsafe(q.put_nowait, payload)
    else:
        # best-effort (sync context)
        try:
            q.put_nowait(payload)
        except Exception:
            pass

# Cookies helpers
from pathlib import Path as _Path


def _cookies_candidates() -> List[Dict[str, Any]]:
    """Return a list of yt-dlp cookie options to try, in order."""
    cfile = _Path(__file__).parent / "cookies" / "cookies.txt"
    cand: List[Dict[str, Any]] = []
    if cfile.exists():
        cand.append({"cookies": str(cfile)})
    # try Chrome default and a couple of common profiles
    cand.append({"cookiesfrombrowser": ("chrome", "Default")})
    cand.append({"cookiesfrombrowser": ("chrome", "Profile 1")})
    cand.append({"cookiesfrombrowser": ("chrome", "Profile 2")})
    # last resort: let yt-dlp auto-pick chrome
    cand.append({"cookiesfrombrowser": ("chrome",)})
    return cand


def _base_opts() -> Dict[str, Any]:
    return {
        "quiet": True,
        "no_color": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": UA_CHROME,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }


def make_hook(job_id: str, loop: asyncio.AbstractEventLoop):
    def hook(d):
        if d["status"] == "downloading":
            percent = d.get("_percent_str"); speed = d.get("_speed_str"); eta = d.get("_eta_str")
            if percent: percent = ansi_escape.sub("", percent).strip()
            if speed:   speed   = ansi_escape.sub("", speed).strip()
            if eta:     eta     = ansi_escape.sub("", eta).strip()
            progress_store[job_id] = {
                "status": "downloading",
                "percent": percent,
                "speed": speed,
                "eta": eta,
                "downloaded_bytes": d.get("downloaded_bytes"),
                "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate"),
            }
            _enqueue_event(job_id, progress_store[job_id], loop)
        elif d["status"] == "finished":
            cur = progress_store.get(job_id, {})
            cur.update({"status": "postprocessing", "percent": "100%"})
            progress_store[job_id] = cur
            _enqueue_event(job_id, progress_store[job_id], loop)
    return hook


def resolve_downloaded_path(ydl: yt_dlp.YoutubeDL, info: Dict[str, Any]) -> str:
    filename = ydl.prepare_filename(info)
    base = os.path.splitext(filename)[0]
    for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".aac"):
        cand = base + ext
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return os.path.abspath(filename)


def normalize_mp4(path: str) -> str:
    """Remux MP4 to a QuickTime‑friendly layout (moov at start, defragmented), no re-encode.
    Returns the (possibly same) path.
    """
    if not path.lower().endswith(".mp4"):
        return path
    fixed = os.path.splitext(path)[0] + ".__fixed__.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        path,
        "-map",
        "0",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",  # корректный аудиобитстрим для MP4
        "-movflags",
        "+faststart",  # moov в начале для совместимости с macOS/браузерами
        fixed,
    ]
    try:
        if shutil.which("ffmpeg") is None:
            return path  # нет ffmpeg — тихо пропускаем
        subprocess.run(cmd, check=True)
        os.replace(fixed, path)
    except Exception:
        try:
            if os.path.exists(fixed):
                os.remove(fixed)
        finally:
            pass
    return path


def _ffprobe_codecs(path: str) -> Dict[str, str]:
    """Return {'vcodec': '...', 'acodec': '...'} using ffprobe; empty strings on failure."""
    vc, ac = "", ""
    try:
        if shutil.which("ffprobe") is None:
            return {"vcodec": vc, "acodec": ac}
        # video codec
        r1 = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
        )
        if r1.returncode == 0:
            vc = (r1.stdout or "").strip()
        # audio codec
        r2 = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
        )
        if r2.returncode == 0:
            ac = (r2.stdout or "").strip()
    except Exception:
        pass
    return {"vcodec": vc, "acodec": ac}


_QUICKTIME_OK_VC = {
    "h264",
    "avc1",
    "hev1",
    "hvc1",
}  # HEVC поддерживается на большинстве современных маков
_QUICKTIME_OK_AC = {"aac", "mp4a", "alac"}


def ensure_quicktime_compatible(path: str) -> str:
    """If codecs are not QuickTime-friendly (e.g., AV1/VP9), transcode video->H.264 (audio->AAC if needed).
    Returns final path. Uses libx264; tries to avoid re-encode unless necessary."""
    if shutil.which("ffmpeg") is None:
        return path
    codecs = _ffprobe_codecs(path)
    v = (codecs.get("vcodec") or "").lower()
    a = (codecs.get("acodec") or "").lower()
    # already ok? just return
    if (v in _QUICKTIME_OK_VC or v == "") and (a in _QUICKTIME_OK_AC or a == ""):
        return path
    # Need compatibility transcode
    out = os.path.splitext(path)[0] + ".__qt__.mp4"
    # choose audio settings: copy if AAC, else transcode to AAC
    ac_args = (
        ["-c:a", "copy"] if a in _QUICKTIME_OK_AC else ["-c:a", "aac", "-b:a", "192k"]
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        *ac_args,
        "-movflags",
        "+faststart",
        out,
    ]
    try:
        subprocess.run(cmd, check=True)
        os.replace(out, path)
    except Exception:
        # cleanup temp
        try:
            if os.path.exists(out):
                os.remove(out)
        finally:
            pass
    return path


async def run_download_async(job_id: str, url: str, fmt: str):
    loop = asyncio.get_running_loop()
    tmpdir = tempfile.mkdtemp(prefix="yt_")

    def _try_download(copt: Dict[str, Any]):
        ydl_opts = _base_opts()
        ydl_opts.update(
            {
                "format": fmt.strip(),
                "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
                "progress_hooks": [make_hook(job_id, loop)],
                "merge_output_format": "mp4",
                "postprocessor_args": {"ffmpeg": ["-movflags", "+faststart"]},
            }
        )
        ydl_opts.update(copt)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return resolve_downloaded_path(ydl, info)

    last_err = None
    try:
        progress_store[job_id] = {"status": "starting"}
        _enqueue_event(job_id, {"status": "starting"}, loop)
        for copt in _cookies_candidates():
            try:
                filepath = await asyncio.to_thread(_try_download, copt)
                break
            except Exception as e:
                last_err = e
                continue
        else:
            if last_err:
                raise last_err
        # extra remux for IG/TikTok peculiar MP4s (no re-encode)
        filepath = await asyncio.to_thread(normalize_mp4, filepath)
        # Ensure QuickTime compatibility only if codecs are not supported; may re-encode video to H.264
        progress_store[job_id] = {
            **progress_store.get(job_id, {}),
            "status": "postprocessing",
            "percent": "100%",
        }
        _enqueue_event(job_id, progress_store[job_id], loop)
        filepath = await asyncio.to_thread(ensure_quicktime_compatible, filepath)
        st = progress_store.get(job_id, {})
        st.update(
            {
                "status": "finished",
                "filepath": filepath,
                "filename": os.path.basename(filepath),
                "percent": "100%",
            }
        )
        progress_store[job_id] = st
        _enqueue_event(job_id, st, loop)
    except Exception as e:
        msg = str(e)
        if any(t in msg for t in ("Instagram", "login required", "rate-limit")):
            msg += " — Instagram may require authentication. Ensure Chrome is unlocked, correct profile is used (Default/Profile 1), or place cookies at app/cookies/cookies.txt."
        progress_store[job_id] = {"status": "error", "error": msg}
        _enqueue_event(job_id, {"status": "error", "error": msg}, loop)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/probe")
async def probe(url: str = Form(...)):
    """
    Отдаёт ВСЕ форматы, которые вернёт yt-dlp, без фильтров.
    Для video-only автоматически добавляем bestaudio, чтобы было со звуком.
    """
    url = url.strip()

    def _extract():
        last_err = None
        for copt in _cookies_candidates():
            ydl_opts = _base_opts()
            ydl_opts.update(copt)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as e:
                last_err = e
                continue
        # if all candidates failed, raise the last error
        if last_err:
            raise last_err

    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _extract)

    raw_formats: List[Dict[str, Any]] = info.get("formats", [])
    out: List[Dict[str, Any]] = []

    for f in raw_formats:
        fid = f.get("format_id")
        if not fid:
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        v_ok = vcodec and vcodec != "none"
        a_ok = acodec and acodec != "none"

        # тип
        if v_ok and a_ok:
            typ = "av"
        elif v_ok:
            typ = "video"
        elif a_ok:
            typ = "audio"
        else:
            typ = "other"

        # подпись
        h, w = f.get("height"), f.get("width")
        fps = f.get("fps")
        ext = f.get("ext")
        tbr = f.get("tbr")
        size = f.get("filesize") or f.get("filesize_approx")

        def _b2s(b):
            if not b:
                return None
            units = ["B", "KB", "MB", "GB", "TB"]
            v = float(b)
            i = 0
            while v >= 1024 and i < len(units) - 1:
                v /= 1024
                i += 1
            return f"{v:.1f} {units[i]}"

        res = f"{w}x{h}" if (w and h) else (f"{h}p" if h else "")
        parts = []
        parts.append(
            "AV"
            if typ == "av"
            else (
                "VIDEO" if typ == "video" else ("AUDIO" if typ == "audio" else "OTHER")
            )
        )
        if res:
            parts.append(res)
        if fps:
            parts.append(f"{int(fps)}fps")
        if ext:
            parts.append(ext)
        label = " • ".join(parts)
        size_s = _b2s(size)
        if size_s:
            label += f" • ~{size_s}"
        if tbr:
            label += f" • {int(tbr)}k"

        # fmt: всегда со звуком для video-only.
        # Для MP4 принудительно предпочитаем AAC/M4A, чтобы итог не ремультиплексировался в MKV.
        fmt = (
            fid
            if typ == "av"
            else (
                f"{fid}+bestaudio[ext=m4a][acodec^=mp4a]/bestaudio[acodec^=mp4a]/bestaudio"
                if typ == "video"
                else fid
            )
        )

        out.append(
            {
                "id": fid,
                "type": typ,
                "label": label,
                "ext": ext,
                "res": res,
                "fps": fps,
                "height": h,
                "tbr": tbr,
                "vcodec": vcodec,
                "acodec": acodec,
                "fmt": fmt,
            }
        )

    # фильтр: оставляем только видео-форматы (av или video) и только mp4
    out = [f for f in out if f["type"] in ("av", "video") and f["ext"] == "mp4"]

    # дедупликация по разрешению: на одно и то же разрешение оставляем лучший вариант
    def _score(fmt: Dict[str, Any]):
        # приоритет: AV (прогрессив) > VIDEO, затем битрейт, затем FPS
        return (
            1 if fmt.get("type") == "av" else 0,
            int(fmt.get("tbr") or 0),
            int(fmt.get("fps") or 0),
        )

    best_by_res: Dict[str, Dict[str, Any]] = {}
    for item in out:
        res_key = item.get("res") or (
            f"{item.get('height')}p" if item.get("height") else ""
        )
        prev = best_by_res.get(res_key)
        if not prev or _score(item) > _score(prev):
            best_by_res[res_key] = item

    out = list(best_by_res.values())

    # простая сортировка
    def _key(x):
        tr = {"av": 0, "video": 1, "audio": 2}.get(x["type"], 3)
        return (tr, -(x["height"] or 0), -(x["tbr"] or 0 if x["tbr"] else 0))

    out.sort(key=_key)
    return JSONResponse(
        {
            "meta": {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
            },
            "formats": out,
        }
    )


@app.post("/download")
async def download(
    url: str = Form(...), fmt: str = Form(...), background_tasks: BackgroundTasks = None
):
    url = url.strip()
    job_id = str(uuid.uuid4())
    progress_store[job_id] = {"status": "queued"}
    if job_id not in job_queues:
        job_queues[job_id] = asyncio.Queue()
    broadcaster_tasks[job_id] = asyncio.create_task(_broadcast_loop(job_id))
    asyncio.create_task(run_download_async(job_id, url, fmt))
    return JSONResponse({"job_id": job_id, "filename": None})


@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    return progress_store.get(job_id, {"status": "unknown"})


@app.get("/fetch/{job_id}")
async def fetch_file(job_id: str):
    info = progress_store.get(job_id)
    if not info or info.get("status") != "finished":
        raise HTTPException(status_code=404, detail="File is not ready yet")
    # файл был сохранён в tmpdir внутри _download; нужно вернуть его
    # проще: использовать resolve_downloaded_path повторно — но надо было сохранить filepath
    # для простоты, сохраним filepath в progress_store в хуке 'finished'
    filepath = info.get("filepath")
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=filepath,
        filename=Path(filepath).name,
        media_type="application/octet-stream",
    )


# WebSocket endpoint for progress updates
@app.websocket("/ws/progress/{job_id}")
async def ws_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    # register
    ws_clients.setdefault(job_id, set()).add(websocket)
    try:
        # send current state immediately if any
        cur = progress_store.get(job_id)
        if cur:
            try:
                await websocket.send_json(cur)
            except Exception:
                pass
        # keep connection alive until client disconnects
        while True:
            # we don't require client messages; receiving helps detect disconnect
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            ws_clients.get(job_id, set()).discard(websocket)
        except Exception:
            pass