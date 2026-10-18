import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

BASE = Path(__file__).parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
MAX_AGE_SECONDS = 3600  # auto-delete files after 1 hour

QUALITIES = {"mp3": None, "360": 360, "720": 720, "1080": 1080, "1440": 1440, "2160": 2160}

app = FastAPI(title="Video Downloader")
jobs: dict[str, dict] = {}
lock = threading.Lock()


class UrlReq(BaseModel):
    url: str


class DownloadReq(BaseModel):
    url: str
    quality: str  # mp3 | 360 | 720 | 1080 | 1440 | 2160


def check_url(url: str) -> str:
    url = url.strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(400, "Please enter a valid http(s) link.")
    return url


def build_opts(quality: str, outdir: Path, hook) -> dict:
    opts = {
        "outtmpl": str(outdir / "%(title).150B.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "concurrent_fragment_downloads": 8,  # speed
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10 * 1024 * 1024,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }
    if quality == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        h = QUALITIES[quality]
        if h <= 1080:
            # Prefer MP4/H.264 for maximum compatibility
            opts["format"] = (
                f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={h}]+ba/b[height<={h}]/b"
            )
            opts["merge_output_format"] = "mp4"
        else:
            # 1440p / 4K are usually VP9/AV1 -> MKV is the safest container
            opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/b"
            opts["merge_output_format"] = "mkv"
    return opts


def run_job(job_id: str, url: str, quality: str):
    outdir = DOWNLOADS / job_id
    outdir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        with lock:
            job = jobs[job_id]
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                job["status"] = "downloading"
                job["percent"] = round(done / total * 100, 1) if total else 0
                job["speed"] = d.get("_speed_str", "").strip()
                job["eta"] = d.get("_eta_str", "").strip()
            elif d["status"] == "finished":
                job["status"] = "processing"
                job["percent"] = 100

    try:
        with yt_dlp.YoutubeDL(build_opts(quality, outdir, hook)) as ydl:
            ydl.download([url])
        files = [f for f in outdir.iterdir() if f.is_file() and f.suffix not in (".part", ".ytdl")]
        if not files:
            raise RuntimeError("No file was produced.")
        final = max(files, key=lambda f: f.stat().st_size)
        with lock:
            jobs[job_id].update(status="done", percent=100, file=str(final), filename=final.name)
    except Exception as e:  # noqa: BLE001
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))  # strip color codes
        with lock:
            jobs[job_id].update(status="error", error=msg[:300])


def cleaner():
    while True:
        time.sleep(300)
        now = time.time()
        for d in DOWNLOADS.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > MAX_AGE_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
                    with lock:
                        jobs.pop(d.name, None)
            except OSError:
                pass


threading.Thread(target=cleaner, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "index.html").read_text(encoding="utf-8")


@app.post("/api/info")
def info(req: UrlReq):
    url = check_url(req.url)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:300])
    heights = sorted({f["height"] for f in data.get("formats", []) if f.get("height")})
    return {
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "max_height": heights[-1] if heights else None,
    }


@app.post("/api/download")
def download(req: DownloadReq):
    url = check_url(req.url)
    if req.quality not in QUALITIES:
        raise HTTPException(400, "Invalid quality.")
    job_id = uuid.uuid4().hex
    with lock:
        jobs[job_id] = {"status": "starting", "percent": 0}
    threading.Thread(target=run_job, args=(job_id, url, req.quality), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return {k: v for k, v in job.items() if k != "file"}


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "File not ready.")
    return FileResponse(job["file"], filename=job["filename"], media_type="application/octet-stream")
      
