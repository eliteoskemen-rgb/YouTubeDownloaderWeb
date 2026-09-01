
import asyncio
import os
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

YTCUI = os.getenv("YTCUI_BIN", "/opt/ytcui-dl/ytcui-dl")

app = FastAPI(title="YouTube Downloader")

jobs = {}


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"
    media_type: str = "video"


def extract_video_id(url: str):
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower()

        if host == "youtu.be":
            value = p.path.strip("/").split("/")[0]
            if value:
                return value

        if host == "youtube.com" or host.endswith(".youtube.com"):
            q = parse_qs(p.query)
            if q.get("v"):
                return q["v"][0]

            parts = [x for x in p.path.split("/") if x]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                return parts[1]
    except Exception:
        pass

    match = re.search(
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{6,})",
        url,
    )
    return match.group(1) if match else None


def normalize_youtube_url(url: str):
    vid = extract_video_id(url)
    if not vid:
        raise ValueError("Не удалось определить YouTube video ID.")
    return f"https://www.youtube.com/watch?v={vid}"


def run_sync(args, timeout=180):
    result = __import__("subprocess").run(
        [YTCUI] + args,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").STDOUT,
        text=True,
        timeout=timeout,
    )
    return result


async def cli_output(args, timeout=180):
    proc = await asyncio.create_subprocess_exec(
        YTCUI,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ytcui-dl превысил время ожидания.")
    return proc.returncode, out.decode("utf-8", "replace")


def parse_qualities(text: str):
    found = set()

    for match in re.finditer(r"(?<!\d)(\d{3,4})p(?:\d{1,3})?", text):
        q = int(match.group(1))
        if 144 <= q <= 4320:
            found.add(q)

    return sorted(found, reverse=True)


async def get_title(url: str):
    rc, text = await cli_output(["--yt-dlp", "-e", url], timeout=90)
    if rc == 0:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if lines:
            return lines[-1]

    return "YouTube video"


async def get_qualities(url: str):
    rc, text = await cli_output(["--diag", url], timeout=150)

    if rc != 0:
        raise RuntimeError(
            "Диагностика ytcui-dl:\n\n" +
            (text[-9000:] or "Нет диагностического вывода")
        )

    qualities = parse_qualities(text)

    if not qualities:
        raise RuntimeError(
            "ytcui-dl не нашёл playable formats.\n\n" +
            text[-9000:]
        )

    return qualities

def safe_file_name(job_id: str, media_type: str):
    return DOWNLOAD_DIR / f"{job_id}.{'mp3' if media_type == 'audio' else 'mp4'}"


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
async def health():
    exists = Path(YTCUI).exists()
    return {
        "status": "ok" if exists else "error",
        "engine": "ytcui-dl",
        "binary": YTCUI,
        "binary_exists": exists,
    }


@app.post("/api/info")
async def info(request: InfoRequest):
    try:
        url = normalize_youtube_url(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        title, qualities = await asyncio.gather(
            get_title(url),
            get_qualities(url),
        )
    except Exception as exc:
        raise HTTPException(502, str(exc))

    video_id = extract_video_id(url)

    formats = [
        {
            "quality": f"{q}p",
            "height": q,
            "file_size_str": "",
        }
        for q in qualities
    ]

    return {
        "success": True,
        "title": title,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        "duration_str": "",
        "formats": formats,
        "audioFormat": "mp3",
    }


async def run_download(job_id: str, url: str, quality: str, media_type: str):
    job = jobs[job_id]
    output = safe_file_name(job_id, media_type)

    try:
        job.update(status="downloading", progress=0, message="Подготавливаю загрузку...")

        if media_type == "audio":
            args = [
                "--yt-dlp",
                "-x",
                "--audio-format", "mp3",
                "-o", str(output),
                url,
            ]
        else:
            if quality == "best":
                # 4320 is requested as the highest ceiling; the selector
                # gracefully falls to the best available working rung.
                args = [
                    "-d",
                    "--remux",
                    "-q", "4320",
                    "-o", str(output),
                    url,
                ]
            else:
                try:
                    height = max(144, min(4320, int(quality.rstrip("p"))))
                except ValueError:
                    height = 1080

                args = [
                    "-d",
                    "--remux",
                    "-q", str(height),
                    "-o", str(output),
                    url,
                ]

        proc = await asyncio.create_subprocess_exec(
            YTCUI,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", "replace").strip()
            job["log"] = text[-1000:]

            m = re.search(r"(\d+(?:\.\d+)?)%", text)
            if m:
                try:
                    job["progress"] = max(
                        0,
                        min(99, float(m.group(1)))
                    )
                except ValueError:
                    pass

            if "ffmpeg" in text.lower() or "remux" in text.lower():
                job["message"] = "Собираю MP4..."
            elif "download" in text.lower():
                job["message"] = "Скачиваю..."

        rc = await proc.wait()

        if rc != 0:
            raise RuntimeError(
                job.get("log")
                or "ytcui-dl завершился с ошибкой."
            )

        # Some builds may use the requested output path directly.
        if not output.exists():
            candidates = [
                p for p in DOWNLOAD_DIR.iterdir()
                if p.is_file() and p.name != "job.json"
            ]
            if candidates:
                output = max(candidates, key=lambda p: p.stat().st_mtime)

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("После загрузки файл не найден.")

        job.update(
            status="finished",
            progress=100,
            message="Готово",
            file=str(output),
            filename=output.name,
        )

    except Exception as exc:
        job.update(
            status="error",
            progress=0,
            message=str(exc),
        )


@app.post("/api/download")
async def download(request: DownloadRequest):
    try:
        url = normalize_youtube_url(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    job_id = uuid.uuid4().hex

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "В очереди...",
        "file": "",
        "filename": "",
        "log": "",
    }

    asyncio.create_task(
        run_download(
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
        raise HTTPException(404, "Job not found")

    result = {
        "success": True,
        **job,
    }

    if job["status"] == "finished":
        result["download_url"] = f"/api/file/{job_id}"

    return result


@app.get("/api/file/{job_id}")
async def file(job_id: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    if job["status"] != "finished":
        raise HTTPException(400, "Файл ещё не готов")

    path = Path(job["file"])

    if not path.exists():
        raise HTTPException(404, "Файл больше не существует")

    return FileResponse(
        path=path,
        filename=job["filename"],
        media_type="application/octet-stream",
    )
