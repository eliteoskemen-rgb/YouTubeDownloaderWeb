import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"

DOWNLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="YouTube Downloader Online")

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static",
)

tasks = {}

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# =========================================================
# yt-dlp
# =========================================================

YT_ARGS = [
    "--js-runtimes",
    "node",

    "--extractor-args",
    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",

    "--no-playlist",
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
# Frontend
# =========================================================

@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


# =========================================================
# Task
# =========================================================

def create_task():
    task_id = uuid.uuid4().hex

    tasks[task_id] = {
        "status": "starting",
        "percent": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "error": "",
        "file": "",
    }

    return task_id


# =========================================================
# Video information
# =========================================================

@app.post("/api/info")
async def info(data: Link):

    url = data.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            status_code=400,
            detail="Неверная ссылка",
        )

    command = [
        "yt-dlp",
        *YT_ARGS,
        "--dump-single-json",
        "--skip-download",
        url,
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    output = stdout.decode("utf-8", "replace")
    error = stderr.decode("utf-8", "replace")

    if process.returncode != 0:

        raise HTTPException(
            status_code=400,
            detail=error[-3000:] or "YouTube не вернул информацию",
        )

    try:
        video = json.loads(output)

    except Exception:

        raise HTTPException(
            status_code=500,
            detail="Не удалось разобрать ответ yt-dlp",
        )

    return {
        "title": video.get("title", "Видео"),
        "uploader": (
            video.get("uploader")
            or video.get("channel")
            or ""
        ),
        "duration": video.get("duration"),
        "height": video.get("height"),
        "thumbnail": video.get("thumbnail", ""),
    }


# =========================================================
# Start download
# =========================================================

@app.post("/api/download")
async def download(data: Download):

    url = data.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            status_code=400,
            detail="Неверная ссылка",
        )

    task_id = create_task()

    asyncio.create_task(
        run_download(task_id, data)
    )

    return {
        "id": task_id
    }


# =========================================================
# Download worker
# =========================================================

async def run_download(task_id, data):

    task = tasks[task_id]

    work = DOWNLOADS / task_id

    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        # -------------------------------------------------
        # AUDIO
        # -------------------------------------------------

        if data.mode == "audio":

            format_args = [
                "-f",
                "bestaudio/best",

                "-x",

                "--audio-format",
                "mp3",

                "--audio-quality",
                "192K",
            ]

        # -------------------------------------------------
        # VIDEO
        # -------------------------------------------------

        else:

            if data.quality == "best":

                format_args = [
                    "-f",
                    "bestvideo+bestaudio/best",

                    "--merge-output-format",
                    "mp4",
                ]

            else:

                try:
                    height = int(data.quality)

                except (TypeError, ValueError):
                    height = 720

                format_args = [
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
        # Output filename
        # -------------------------------------------------

        output = str(
            work / "%(title)s.%(ext)s"
        )

        command = [
            "yt-dlp",

            *YT_ARGS,

            "--newline",
            "--progress",

            *format_args,

            "-o",
            output,

            data.url,
        ]

        # -------------------------------------------------
        # FFmpeg
        # -------------------------------------------------

        ffmpeg = shutil.which("ffmpeg")

        if ffmpeg:

            command[command.index(data.url):command.index(data.url)] = [
                "--ffmpeg-location",
                ffmpeg,
            ]

        # -------------------------------------------------
        # Start yt-dlp
        # -------------------------------------------------

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,
        )

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                "utf-8",
                "replace",
            ).strip()

            # Progress
            progress = re.search(
                r"\[download\]\s+([\d.]+)%",
                text,
            )

            if progress:

                task["percent"] = float(
                    progress.group(1)
                )

                task["status"] = "downloading"

            # Speed
            speed = re.search(
                r"\bat\s+(.+?)(?:\s+ETA\s+(.+?))?(?:\s*$)",
                text,
            )

            if speed:

                task["speed"] = speed.group(1)

                if speed.group(2):
                    task["eta"] = speed.group(2)

            # Filename
            if "Destination:" in text:

                task["status"] = "processing"

                task["filename"] = (
                    text.split("Destination:", 1)[1]
                    .strip()
                )

            if "[Merger]" in text:

                task["status"] = "processing"

        return_code = await process.wait()

        if return_code != 0:

            raise RuntimeError(
                "yt-dlp не смог скачать видео. "
                "Проверь сообщение YouTube ниже."
            )

        # -------------------------------------------------
        # Find final file
        # -------------------------------------------------

        files = [
            path
            for path in work.iterdir()
            if path.is_file()
            and not path.name.endswith(".part")
        ]

        if not files:

            raise RuntimeError(
                "После загрузки файл не найден."
            )

        final_file = max(
            files,
            key=lambda path: path.stat().st_mtime,
        )

        task.update(
            {
                "status": "done",
                "percent": 100,
                "filename": final_file.name,
                "file": str(final_file),
                "error": "",
            }
        )

    except Exception as exc:

        task.update(
            {
                "status": "error",
                "error": str(exc),
            }
        )


# =========================================================
# Progress
# =========================================================

@app.get("/api/progress/{task_id}")
def progress(task_id: str):

    if task_id not in tasks:

        raise HTTPException(
            status_code=404,
            detail="Задача не найдена",
        )

    return tasks[task_id]


# =========================================================
# File
# =========================================================

@app.get("/api/file/{task_id}")
def get_file(task_id: str):

    task = tasks.get(task_id)

    if (
        not task
        or task["status"] != "done"
        or not task["file"]
    ):

        raise HTTPException(
            status_code=404,
            detail="Файл ещё не готов",
        )

    file_path = Path(task["file"])

    if not file_path.exists():

        raise HTTPException(
            status_code=404,
            detail="Файл удалён",
        )

    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# =========================================================
# Health check
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "node": bool(shutil.which("node")),
    }