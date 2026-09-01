import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


# =========================================================
# Paths
# =========================================================

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"

DOWNLOADS.mkdir(
    parents=True,
    exist_ok=True,
)


# =========================================================
# App
# =========================================================

app = FastAPI(
    title="YouTube Downloader Online"
)


# =========================================================
# State
# =========================================================

tasks = {}


# =========================================================
# URL validation
# =========================================================

URL_RE = re.compile(
    r"^https?://",
    re.IGNORECASE,
)


# =========================================================
# yt-dlp configuration
# =========================================================

YT_ARGS = [
    # YouTube JS challenge solving
    "--js-runtimes",
    "node",

    # bgutil PO Token provider
    "--extractor-args",
    (
        "youtubepot-bgutilhttp:"
        "base_url=http://127.0.0.1:4416;"
        "youtube:player-client=mweb"
    ),

    # Do not download playlists
    "--no-playlist",

    # Network reliability
    "--retries",
    "3",

    "--fragment-retries",
    "3",

    "--socket-timeout",
    "30",

    # Avoid unnecessary warnings
    "--no-warnings",
]


# =========================================================
# Models
# =========================================================

class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


# =========================================================
# Health
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "youtube-downloader",
    }


# =========================================================
# Frontend
# =========================================================

@app.get("/")
def index():
    return FileResponse(
        BASE / "index.html"
    )


# =========================================================
# Task creation
# =========================================================

def create_task():
    tid = uuid.uuid4().hex

    tasks[tid] = {
        "status": "starting",
        "percent": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "error": "",
        "file": "",
    }

    return tid


# =========================================================
# yt-dlp runner
# =========================================================

async def run_process(cmd):

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output = []

    while True:

        line = await process.stdout.readline()

        if not line:
            break

        text = line.decode(
            "utf-8",
            "replace",
        ).strip()

        if text:
            output.append(text)

            # Keep memory bounded
            if len(output) > 200:
                output.pop(0)

    return_code = await process.wait()

    return return_code, output


# =========================================================
# Video information
# =========================================================

@app.post("/api/info")
async def info(x: Link):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            400,
            "Неверная ссылка",
        )

    cmd = [
        "yt-dlp",

        *YT_ARGS,

        "--dump-single-json",
        "--skip-download",

        url,
    ]

    return_code, output = await run_process(cmd)

    if return_code != 0:

        error = "\n".join(
            output[-40:]
        )

        raise HTTPException(
            400,
            error or "Не удалось получить информацию о видео",
        )

    raw = "\n".join(output)

    # yt-dlp JSON can be surrounded by warnings.
    # Find the JSON object.
    start = raw.find("{")
    end = raw.rfind("}")

    if start == -1 or end == -1:
        raise HTTPException(
            500,
            "yt-dlp не вернул JSON",
        )

    try:

        data = json.loads(
            raw[start:end + 1]
        )

    except Exception:

        raise HTTPException(
            500,
            "Не удалось разобрать ответ yt-dlp",
        )

    return {
        "id": data.get("id", ""),
        "title": data.get(
            "title",
            "Видео",
        ),
        "uploader": (
            data.get("uploader")
            or data.get("channel")
            or ""
        ),
        "duration": data.get(
            "duration"
        ),
        "height": data.get(
            "height"
        ),
        "thumbnail": data.get(
            "thumbnail",
            "",
        ),
        "webpage_url": data.get(
            "webpage_url",
            url,
        ),
    }


# =========================================================
# Start download
# =========================================================

@app.post("/api/download")
async def download(x: Download):

    url = x.url.strip()

    if not URL_RE.match(url):

        raise HTTPException(
            400,
            "Неверная ссылка",
        )

    tid = create_task()

    asyncio.create_task(
        run_download(
            tid,
            x,
        )
    )

    return {
        "id": tid,
    }


# =========================================================
# Download worker
# =========================================================

async def run_download(
    tid,
    x,
):

    task = tasks[tid]

    work = DOWNLOADS / tid

    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        # -------------------------------------------------
        # Audio
        # -------------------------------------------------

        if x.mode == "audio":

            fmt = [
                "-f",
                "bestaudio/best",

                "-x",

                "--audio-format",
                "mp3",

                "--audio-quality",
                "192K",
            ]

        # -------------------------------------------------
        # Video
        # -------------------------------------------------

        else:

            if x.quality == "best":

                fmt = [
                    "-f",
                    (
                        "bestvideo+bestaudio/"
                        "best"
                    ),

                    "--merge-output-format",
                    "mp4",
                ]

            else:

                try:
                    height = int(
                        x.quality
                    )

                except ValueError:
                    height = 720

                fmt = [
                    "-f",
                    (
                        f"bestvideo[height<={height}]"
                        f"+bestaudio/"
                        f"best[height<={height}]"
                        f"/best"
                    ),

                    "--merge-output-format",
                    "mp4",
                ]

        # -------------------------------------------------
        # Output
        # -------------------------------------------------

        output = str(
            work / "%(title)s.%(ext)s"
        )

        ffmpeg = shutil.which(
            "ffmpeg"
        )

        cmd = [
            "yt-dlp",

            *YT_ARGS,

            "--newline",
            "--progress",

            *fmt,

            "-o",
            output,
        ]

        if ffmpeg:

            cmd.extend(
                [
                    "--ffmpeg-location",
                    ffmpeg,
                ]
            )

        cmd.append(
            x.url
        )

        # -------------------------------------------------
        # Start yt-dlp
        # -------------------------------------------------

        process = await asyncio.create_subprocess_exec(
            *cmd,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,
        )

        logs = []

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                "utf-8",
                "replace",
            ).strip()

            if not text:
                continue

            logs.append(text)

            if len(logs) > 100:
                logs.pop(0)

            # ---------------------------------------------
            # Download progress
            # ---------------------------------------------

            match = re.search(
                r"\[download\]\s+"
                r"([\d.]+)%"
                r".*?"
                r"at\s+(.+?)\s+"
                r"ETA\s+(.+)",
                text,
            )

            if match:

                try:

                    task["percent"] = float(
                        match.group(1)
                    )

                except ValueError:
                    pass

                task["speed"] = (
                    match.group(2)
                )

                task["eta"] = (
                    match.group(3)
                )

                task["status"] = (
                    "downloading"
                )

            # ---------------------------------------------
            # Processing
            # ---------------------------------------------

            if (
                "Destination:" in text
                or "[Merger]" in text
                or "[ExtractAudio]" in text
                or "[ffmpeg]" in text
            ):

                task["status"] = (
                    "processing"
                )

        return_code = await process.wait()

        if return_code != 0:

            error = "\n".join(
                logs[-30:]
            )

            raise RuntimeError(
                error
                or "yt-dlp завершился с ошибкой"
            )

        # -------------------------------------------------
        # Find file
        # -------------------------------------------------

        candidates = [
            p
            for p in work.iterdir()
            if p.is_file()
        ]

        if not candidates:

            raise RuntimeError(
                "yt-dlp завершился успешно, "
                "но готовый файл не найден"
            )

        # Prefer final media files
        media = [
            p
            for p in candidates
            if p.suffix.lower()
            in {
                ".mp4",
                ".mkv",
                ".webm",
                ".mp3",
                ".m4a",
                ".opus",
            }
        ]

        if media:
            candidates = media

        file_path = max(
            candidates,
            key=lambda p: p.stat().st_mtime,
        )

        task.update(
            {
                "status": "done",
                "percent": 100,
                "filename": file_path.name,
                "file": str(file_path),
                "error": "",
            }
        )

    except Exception as error:

        task.update(
            {
                "status": "error",
                "error": str(error)[-5000:],
            }
        )


# =========================================================
# Progress
# =========================================================

@app.get("/api/progress/{tid}")
def progress(tid: str):

    if tid not in tasks:

        raise HTTPException(
            404,
            "Задача не найдена",
        )

    return tasks[tid]


# =========================================================
# File
# =========================================================

@app.get("/api/file/{tid}")
def file(tid: str):

    task = tasks.get(tid)

    if (
        not task
        or task["status"] != "done"
        or not task["file"]
    ):

        raise HTTPException(
            404,
            "Файл ещё не готов",
        )

    file_path = Path(
        task["file"]
    )

    if not file_path.exists():

        raise HTTPException(
            404,
            "Файл удалён",
        )

    return FileResponse(
        file_path,

        filename=file_path.name,

        media_type=(
            "application/octet-stream"
        ),
    )