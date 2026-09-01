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

URL_RE = re.compile(r"^https?://", re.I)


# =========================================================
# YT-DLP
# =========================================================

YT_ARGS = [
    "--js-runtimes",
    "node",
    "--no-playlist",
]


# =========================================================
# MODELS
# =========================================================

class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


# =========================================================
# TASK
# =========================================================

def safe_task():
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
# VIDEO INFO
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

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    out, err = await process.communicate()

    if process.returncode != 0:

        error = err.decode(
            "utf-8",
            "replace",
        )

        raise HTTPException(
            400,
            error[-3000:],
        )

    try:
        data = json.loads(out)

    except Exception:

        raise HTTPException(
            500,
            "Не удалось получить информацию о видео",
        )

    return {
        "title": data.get("title", "Видео"),
        "uploader": (
            data.get("uploader")
            or data.get("channel")
            or ""
        ),
        "duration": data.get("duration"),
        "height": data.get("height"),
        "thumbnail": data.get("thumbnail", ""),
    }


# =========================================================
# START DOWNLOAD
# =========================================================

@app.post("/api/download")
async def download(x: Download):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            400,
            "Неверная ссылка",
        )

    tid = safe_task()

    asyncio.create_task(
        run_download(tid, x)
    )

    return {
        "id": tid
    }


# =========================================================
# DOWNLOAD
# =========================================================

async def run_download(tid, x):

    task = tasks[tid]

    work = DOWNLOADS / tid

    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        # -------------------------------------------------
        # AUDIO
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
        # VIDEO
        # -------------------------------------------------

        else:

            if x.quality == "best":

                fmt = [
                    "-f",
                    "bestvideo*+bestaudio/best",

                    "--merge-output-format",
                    "mp4",
                ]

            else:

                try:
                    height = int(x.quality)

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
        # OUTPUT
        # -------------------------------------------------

        output = str(
            work / "%(title)s.%(ext)s"
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

        ffmpeg = shutil.which("ffmpeg")

        if ffmpeg:

            cmd.extend([
                "--ffmpeg-location",
                ffmpeg,
            ])

        cmd.append(url)

        # -------------------------------------------------
        # PROCESS
        # -------------------------------------------------

        process = await asyncio.create_subprocess_exec(
            *cmd,

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

            # ---------------------------------------------
            # SAVE LAST ERROR/OUTPUT
            # ---------------------------------------------

            if "ERROR:" in text:

                task["error"] = text

            # ---------------------------------------------
            # PROGRESS
            # ---------------------------------------------

            match = re.search(
                r"\[download\]\s+([\d.]+)%",
                text,
            )

            if match:

                task["percent"] = float(
                    match.group(1)
                )

                task["status"] = "downloading"

            speed = re.search(
                r"at\s+(.+?)\s+ETA\s+(.+)",
                text,
            )

            if speed:

                task["speed"] = speed.group(1)
                task["eta"] = speed.group(2)

            # ---------------------------------------------
            # PROCESSING
            # ---------------------------------------------

            if (
                "Destination:" in text
                or "[Merger]" in text
                or "[ExtractAudio]" in text
            ):

                task["status"] = "processing"

        return_code = await process.wait()

        # -------------------------------------------------
        # ERROR
        # -------------------------------------------------

        if return_code != 0:

            error = task["error"]

            if not error:
                error = (
                    "yt-dlp завершился с ошибкой. "
                    "YouTube мог заблокировать запрос."
                )

            raise RuntimeError(error)

        # -------------------------------------------------
        # FIND FILE
        # -------------------------------------------------

        candidates = [
            p
            for p in work.iterdir()
            if p.is_file()
        ]

        if not candidates:

            raise RuntimeError(
                "Файл не найден после загрузки"
            )

        file_path = max(
            candidates,
            key=lambda p: p.stat().st_mtime,
        )

        task.update({
            "status": "done",
            "percent": 100,
            "filename": file_path.name,
            "file": str(file_path),
            "error": "",
        })

    except Exception as error:

        task.update({
            "status": "error",
            "error": str(error),
        })


# =========================================================
# PROGRESS
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
# FILE
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

    file_path = Path(task["file"])

    if not file_path.exists():

        raise HTTPException(
            404,
            "Файл удалён",
        )

    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )