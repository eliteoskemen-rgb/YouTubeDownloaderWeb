import asyncio
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


app = FastAPI(title="YouTube Downloader")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
        host = (parsed.hostname or "").lower()

        if host in ("youtu.be", "www.youtu.be"):
            return parsed.path.strip("/").split("/")[0]

        if "youtube.com" in host:
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
        url,
    )

    return match.group(1) if match else None


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

        # JS runtime
        "--js-runtimes",
        "node",

        # PO Token provider
        "--extractor-args",
        f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}",

        # YouTube clients
        "--extractor-args",
        "youtube:player-client=mweb,web_embedded,tv",

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
            result.stderr[-5000:]
            or "yt-dlp metadata error"
        )

    return json.loads(result.stdout)


def job_dir(job_id: str):
    path = DOWNLOAD_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_file(job_id: str):
    return job_dir(job_id) / "job.json"


def save_job(job_id: str):
    path = job_file(job_id)

    temp = path.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            jobs[job_id],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temp.replace(path)


def load_job(job_id: str):
    if job_id in jobs:
        return jobs[job_id]

    path = job_file(job_id)

    if not path.exists():
        return None

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
        jobs[job_id] = data
        return data
    except Exception:
        return None


def update_job(job_id: str, **values):
    job = load_job(job_id)

    if not job:
        return

    job.update(values)
    save_job(job_id)


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
            detail=str(e),
        )


def download_video(job_id, url, quality, media_type):
    update_job(
        job_id,
        status="downloading",
        progress=0,
        message="Связываюсь с YouTube...",
    )

    temp_dir = job_dir(job_id)

    output_template = str(
        temp_dir / "%(title).120s.%(ext)s"
    )

    if media_type == "audio":

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
            fmt = "bestvideo+bestaudio/best"

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
            p
            for p in temp_dir.iterdir()
            if p.is_file()
            and p.name != "job.json"
            and not p.name.endswith(".tmp")
        ]

        if not files:
            raise RuntimeError(
                "yt-dlp завершился без созданного файла"
            )

        output_file = max(
            files,
            key=lambda p: p.stat().st_mtime,
        )

        update_job(
            job_id,
            status="finished",
            progress=100,
            message="Готово",
            file=str(output_file),
            filename=output_file.name,
        )

    except Exception as e:

        update_job(
            job_id,
            status="error",
            progress=0,
            message=str(e),
        )


@app.post("/api/download")
async def start_download(request: DownloadRequest):

    try:
        url = normalize_youtube_url(request.url)

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    job_id = uuid.uuid4().hex

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "В очереди...",
        "file": None,
        "filename": None,
    }

    save_job(job_id)

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

    job = load_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found",
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

    job = load_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    if job["status"] != "finished":
        raise HTTPException(
            status_code=400,
            detail="Файл ещё не готов",
        )

    file_path = Path(job["file"])

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Файл больше не существует",
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