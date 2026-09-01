import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel


app = FastAPI(title="YouTube Downloader")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

POT_PROVIDER_URL = os.getenv(
    "POT_PROVIDER_URL",
    "http://127.0.0.1:4416"
)

jobs = {}


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"
    media_type: str = "video"


def extract_video_id(url: str):
    try:
        parsed = urlparse(url)

        if parsed.hostname in ("youtu.be", "www.youtu.be"):
            return parsed.path.strip("/").split("/")[0]

        if parsed.hostname and "youtube.com" in parsed.hostname:
            query = parse_qs(parsed.query)

            if "v" in query:
                return query["v"][0]

            parts = parsed.path.strip("/").split("/")

            if len(parts) >= 2 and parts[0] in (
                "shorts",
                "embed",
                "live",
            ):
                return parts[1]

    except Exception:
        pass

    match = re.search(
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{6,})",
        url
    )

    if match:
        return match.group(1)

    return None


def normalize_youtube_url(url: str):
    video_id = extract_video_id(url)

    if not video_id:
        raise ValueError("Не удалось определить YouTube video ID")

    return f"https://www.youtube.com/watch?v={video_id}"


def get_thumbnail(video_id: str):
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def yt_dlp_base_args():
    return [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--ignore-config",

        # JavaScript/EJS support
        "--js-runtimes",
        "node",

        # Automatically use our PO token provider
        "--extractor-args",
        "youtube:player-client=mweb,web_embedded,tv",

        # Don't use account cookies
        "--no-check-certificates",
    ]


def run_yt_dlp(args, timeout=120):
    command = yt_dlp_base_args() + args

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    return result


def metadata_from_yt_dlp(url: str):
    result = run_yt_dlp(
        [
            "--dump-single-json",
            "--skip-download",
            url,
        ],
        timeout=90,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-5000:] or "yt-dlp metadata error"
        )

    return json.loads(result.stdout)


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "youtube-downloader",
        "pot_provider": POT_PROVIDER_URL,
    }


@app.get("/api/health")
async def api_health():
    return {
        "status": "ok",
        "pot_provider": POT_PROVIDER_URL,
    }


@app.post("/api/info")
async def get_info(request: InfoRequest):
    try:
        url = normalize_youtube_url(request.url)
        video_id = extract_video_id(url)

        # First get information through yt-dlp
        try:
            data = await asyncio.to_thread(
                metadata_from_yt_dlp,
                url,
            )

            return {
                "success": True,
                "id": video_id,
                "title": data.get("title") or "YouTube video",
                "thumbnail": (
                    data.get("thumbnail")
                    or get_thumbnail(video_id)
                ),
                "duration": data.get("duration"),
                "channel": data.get("channel"),
                "uploader": data.get("uploader"),
                "webpage_url": url,
            }

        except Exception:
            # Thumbnail still works even if YouTube blocks yt-dlp
            return {
                "success": True,
                "id": video_id,
                "title": "YouTube video",
                "thumbnail": get_thumbnail(video_id),
                "duration": None,
                "channel": None,
                "uploader": None,
                "webpage_url": url,
                "metadata_limited": True,
            }

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


def download_video(job_id, url, quality, media_type):
    job = jobs[job_id]

    job["status"] = "downloading"
    job["progress"] = 0
    job["message"] = "Подготавливаю загрузку..."

    temp_dir = DOWNLOAD_DIR / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    if media_type == "audio":
        output_template = str(
            temp_dir / "%(title).120s.%(ext)s"
        )

        args = [
            "-f",
            "bestaudio/best",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "192K",
            "-o",
            output_template,
            url,
        ]

    else:
        output_template = str(
            temp_dir / "%(title).120s.%(ext)s"
        )

        if quality == "1080p":
            fmt = (
                "bestvideo[height<=1080]+bestaudio/"
                "best[height<=1080]/best"
            )

        elif quality == "720p":
            fmt = (
                "bestvideo[height<=720]+bestaudio/"
                "best[height<=720]/best"
            )

        elif quality == "480p":
            fmt = (
                "bestvideo[height<=480]+bestaudio/"
                "best[height<=480]/best"
            )

        else:
            fmt = (
                "bestvideo+bestaudio/"
                "best"
            )

        args = [
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "-o",
            output_template,
            url,
        ]

    try:
        job["message"] = "Связываюсь с YouTube..."

        result = run_yt_dlp(
            args,
            timeout=600,
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr[-8000:]
                or "Download failed"
            )

        files = [
            p for p in temp_dir.iterdir()
            if p.is_file()
        ]

        if not files:
            raise RuntimeError(
                "yt-dlp завершился без созданного файла"
            )

        output_file = max(
            files,
            key=lambda p: p.stat().st_mtime
        )

        job["status"] = "finished"
        job["progress"] = 100
        job["message"] = "Готово"
        job["file"] = str(output_file)
        job["filename"] = output_file.name

    except Exception as e:
        job["status"] = "error"
        job["message"] = str(e)


@app.post("/api/download")
async def start_download(request: DownloadRequest):
    try:
        url = normalize_youtube_url(request.url)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )

    job_id = uuid.uuid4().hex

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "В очереди...",
        "file": None,
        "filename": None,
    }

    asyncio.create_task(
        asyncio.to_thread(
            download_video,
            job_id,
            url,
            request.quality,
            request.media_type,
        )
    )

    return {
        "success": True,
        "job_id": job_id,
    }


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    response = {
        "success": True,
        **job,
    }

    if job["status"] == "finished":
        response["download_url"] = (
            f"/api/file/{job_id}"
        )

    return response


@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    if job["status"] != "finished":
        raise HTTPException(
            status_code=400,
            detail="Файл ещё не готов"
        )

    file_path = Path(job["file"])

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Файл больше не существует"
        )

    return FileResponse(
        path=file_path,
        filename=job["filename"],
        media_type="application/octet-stream",
    )


@app.get("/api/debug/yt-dlp")
async def debug_yt_dlp():
    result = subprocess.run(
        [
            "yt-dlp",
            "--version",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    return {
        "yt_dlp": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "pot_provider": POT_PROVIDER_URL,
    }