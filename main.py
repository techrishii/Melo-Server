from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Audio Service",
    description="Submit a public media URL and download the converted MP3 when the job finishes.",
    version="1.0.0",
)

# This registry is intentionally process-local. Use Redis/database-backed jobs and a worker queue
# when deploying more than one application process or container.
jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()

storage_dir = Path(os.getenv("AUDIO_STORAGE_DIR", "storage")).resolve()
work_dir = storage_dir / "work"
output_dir = storage_dir / "output"
work_dir.mkdir(parents=True, exist_ok=True)
output_dir.mkdir(parents=True, exist_ok=True)

mp3_bitrate = os.getenv("MP3_BITRATE", "320k")
ffmpeg_timeout_seconds = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "1800"))
max_download_bytes = int(os.getenv("MAX_DOWNLOAD_BYTES", "0"))

# Prefer an explicit deployment setting. The Windows fallback supports the common
# local FFmpeg extraction location without requiring a PATH restart.
windows_ffmpeg = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
ffmpeg_binary = os.getenv("FFMPEG_BINARY") or (
    str(windows_ffmpeg) if windows_ffmpeg.is_file() else "ffmpeg"
)

# Render Secret Files are mounted under /etc/secrets.
youtube_cookie_file = Path(
    os.getenv("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
)

JobStatus = Literal["queued", "processing", "complete", "failed"]


class CreateJobRequest(BaseModel):
    source_url: HttpUrl


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    source_url: str
    download_url: str | None
    output_filename: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


def now() -> datetime:
    return datetime.now(timezone.utc)


def update_job(job_id: str, **changes: object) -> None:
    """Update a job atomically and refresh its timestamp."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(changes)
            job["updated_at"] = now()


def public_job(job: dict) -> dict:
    """Return only fields intended for API clients."""
    return {
        key: job[key]
        for key in (
            "id", "status", "source_url", "download_url", "output_filename", "error",
            "created_at", "updated_at",
        )
    }


def make_mp3_filename(title: str) -> str:
    """Keep the provider title while making it safe for an Android download filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")
    if not cleaned:
        cleaned = "audio"
    return f"{cleaned[:180].rstrip('. ')}.mp3"


def download_source(job_id: str, source_url: str) -> tuple[Path, str]:
    """Download the best available audio stream into this job's private work folder."""
    job_work_dir = work_dir / job_id
    job_work_dir.mkdir(parents=True, exist_ok=False)

    ydl_options: dict = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(job_work_dir / "source.%(ext)s"),
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }

    # Copy the read-only Render secret to the writable job directory.
    youtube_cookie_file = Path("/etc/secrets/youtube-cookies.txt")

    if youtube_cookie_file.is_file():
        writable_cookie_file = job_work_dir / "youtube-cookies.txt"
        shutil.copyfile(youtube_cookie_file, writable_cookie_file)

        ydl_options["cookiefile"] = str(writable_cookie_file)
        logger.info("YouTube cookies loaded from the Render secret file.")
    else:
        logger.warning("YouTube cookies file not found at %s", youtube_cookie_file)

    if max_download_bytes > 0:
        ydl_options["max_filesize"] = max_download_bytes

    with yt_dlp.YoutubeDL(ydl_options) as downloader:
        info = downloader.extract_info(source_url, download=True)
        if info is None:
            raise RuntimeError("The media provider returned no downloadable media.")

        # yt-dlp may return a playlist structure even with noplaylist enabled for some extractors.
        if "entries" in info:
            entries = [entry for entry in info["entries"] if entry]
            if len(entries) != 1:
                raise RuntimeError("The URL must resolve to exactly one media item.")
            info = entries[0]

        requested_downloads = info.get("requested_downloads") or []
        downloaded_name = requested_downloads[0].get("filepath") if requested_downloads else None
        downloaded_path = Path(downloaded_name) if downloaded_name else Path(downloader.prepare_filename(info))

    if not downloaded_path.is_file():
        raise RuntimeError("The media download did not produce an audio file.")
    title = str(info.get("title") or "audio")
    return downloaded_path, title


def convert_to_mp3(input_path: Path, output_path: Path) -> None:
    """Convert or extract the first audio stream using the system FFmpeg executable."""
    command = [
        ffmpeg_binary,
        "-nostdin",
        "-y",
        "-i", str(input_path),
        "-map", "0:a:0",
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", mp3_bitrate,
        str(output_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=ffmpeg_timeout_seconds,
        )
    except FileNotFoundError as error:
        raise RuntimeError("FFmpeg is not installed or is not available on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("FFmpeg conversion timed out.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "FFmpeg could not convert this media."
        raise RuntimeError(tail[:300]) from error

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not create an MP3 file.")


def process_job(job_id: str) -> None:
    """Background pipeline: download a URL with yt-dlp, then convert it with FFmpeg."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        source_url = job["source_url"]

    update_job(job_id, status="processing", error=None)
    job_work_dir = work_dir / job_id
    output_path = output_dir / f"{job_id}.mp3"

    try:
        source_path, source_title = download_source(job_id, source_url)
        output_filename = make_mp3_filename(source_title)
        convert_to_mp3(source_path, output_path)
        update_job(
            job_id,
            status="complete",
            output_path=output_path,
            output_filename=output_filename,
            download_url=f"/downloads/{job_id}",
        )
    except Exception as error:  # Keep background-task failures visible through the job API.
        logger.exception("Audio job %s failed", job_id)
        if output_path.exists():
            output_path.unlink()
        update_job(job_id, status="failed", error=str(error)[:500])
    finally:
        shutil.rmtree(job_work_dir, ignore_errors=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", status_code=202, response_model=CreateJobResponse)
def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks) -> CreateJobResponse:
    job_id = str(uuid4())
    created_at = now()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "source_url": str(request.source_url),
            "download_url": None,
            "output_path": None,
            "output_filename": None,
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
        }

    background_tasks.add_task(process_job, job_id)
    return CreateJobResponse(job_id=job_id, status="queued", status_url=f"/jobs/{job_id}")


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobResponse(**public_job(job))


@app.get("/downloads/{job_id}")
def download_file(job_id: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] != "complete":
            raise HTTPException(status_code=409, detail="The MP3 is not ready")
        output_path = job["output_path"]
        output_filename = job["output_filename"]

    if not isinstance(output_path, Path) or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Converted MP3 was not found")

    if not isinstance(output_filename, str):
        output_filename = f"audio-{job_id}.mp3"
    return FileResponse(output_path, media_type="audio/mpeg", filename=output_filename)
