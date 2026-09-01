import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# =========================================================
# CONFIG
# =========================================================

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"

DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="YouTube Downloader Online")

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static"
)

tasks = {}

URL_RE = re.compile(r"^https?://", re.I)


# =========================================================
# YT-DLP
# =========================================================

# ВАЖНО:
# Cookies больше НЕ используются.
# Старый вариант с /etc/secrets/*.txt удалён,
# потому что Render делает Secret Files read-only,
# а YouTube может инвалидировать cookies.

YT_BASE_ARGS = [
    "--js-runtimes",
    "deno",
    "--no-playlist",
]


# Дополнительный вариант без cookies.
# TV-клиент сейчас полезен как fallback для публичных видео.
YT_FALLBACK_ARGS = [
    "--extractor-args",
    "youtube:player_client=tv",
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
# HELPERS
# =========================================================

def validate_url(url: str):
    url = url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

    return url


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


async def run_ytdlp(args):
    """
    Запускает yt-dlp и возвращает:
    returncode, stdout/stderr.
    """

    p = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    out, err = await p.communicate()

    return (
        p.returncode,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


# =========================================================
# INDEX
# =========================================================

@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


# =========================================================
# VIDEO INFO
# =========================================================

@app.post("/api/info")
async def info(x: Link):

    url = validate_url(x.url)

    # -----------------------------------------------------
    # Попытка №1
    # Обычный yt-dlp без cookies
    # -----------------------------------------------------

    cmd = [
        "yt-dlp",
        *YT_BASE_ARGS,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        url,
    ]

    rc, out, err = await run_ytdlp(cmd)

    # -----------------------------------------------------
    # Попытка №2
    # TV client
    # -----------------------------------------------------

    if rc != 0:

        cmd = [
            "yt-dlp",
            *YT_BASE_ARGS,
            *YT_FALLBACK_ARGS,
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            url,
        ]

        rc, out, err = await run_ytdlp(cmd)

    # -----------------------------------------------------
    # Ошибка
    # -----------------------------------------------------

    if rc != 0:

        error_text = (err or out).strip()

        raise HTTPException(
            400,
            error_text[-2500:] or "Не удалось получить информацию о видео"
        )

    # -----------------------------------------------------
    # JSON
    # -----------------------------------------------------

    try:
        data = json.loads(out)
    except Exception:

        raise HTTPException(
            500,
            "yt-dlp вернул некорректные данные"
        )

    return {
        "title": data.get("title") or "Видео",
        "uploader": (
            data.get("uploader")
            or data.get("channel")
            or ""
        ),
        "duration": data.get("duration"),
        "height": data.get("height"),
        "thumbnail": data.get("thumbnail") or "",
    }


# =========================================================
# START DOWNLOAD
# =========================================================

@app.post("/api/download")
async def download(x: Download):

    url = validate_url(x.url)

    tid = safe_task()

    asyncio.create_task(
        run_download(
            tid,
            x
        )
    )

    return {
        "id": tid
    }


# =========================================================
# DOWNLOAD
# =========================================================

async def run_download(tid, x):

    t = tasks[tid]

    work = DOWNLOADS / tid
    work.mkdir(parents=True, exist_ok=True)

    try:

        # -------------------------------------------------
        # FORMAT
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

        else:

            if x.quality == "best":

                fmt = [
                    "-f",
                    "bestvideo+bestaudio/best",
                    "--merge-output-format",
                    "mp4",
                ]

            else:

                try:
                    height = int(x.quality)
                except Exception:
                    height = 720

                fmt = [
                    "-f",
                    (
                        f"bestvideo[height<={height}]"
                        f"+bestaudio/"
                        f"best[height<={height}]/"
                        f"best"
                    ),
                    "--merge-output-format",
                    "mp4",
                ]

        # -------------------------------------------------
        # FFMPEG
        # -------------------------------------------------

        ffmpeg = shutil.which("ffmpeg")

        # -------------------------------------------------
        # OUTPUT
        # -------------------------------------------------

        output = str(
            work / "%(title)s.%(ext)s"
        )

        # -------------------------------------------------
        # MAIN COMMAND
        # -------------------------------------------------

        cmd = [
            "yt-dlp",

            *YT_BASE_ARGS,

            "--newline",
            "--progress",

            *fmt,

            "-o",
            output,

            x.url,
        ]

        if ffmpeg:
            cmd.extend([
                "--ffmpeg-location",
                ffmpeg,
            ])

        # -------------------------------------------------
        # RUN
        # -------------------------------------------------

        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:

            line = await p.stdout.readline()

            if not line:
                break

            s = line.decode(
                "utf-8",
                "replace"
            ).strip()

            # ---------------------------------------------
            # PROGRESS
            # ---------------------------------------------

            m = re.search(
                r"\[download\]\s+([\d.]+)%.*?at\s+(.+?)\s+ETA\s+(.+)",
                s
            )

            if m:

                try:
                    t["percent"] = float(
                        m.group(1)
                    )
                except Exception:
                    pass

                t["speed"] = m.group(2)
                t["eta"] = m.group(3)

            # ---------------------------------------------
            # FILENAME
            # ---------------------------------------------

            if "Destination:" in s:

                filename = s.split(
                    "Destination:",
                    1
                )[-1].strip()

                t["filename"] = Path(
                    filename
                ).name

            # ---------------------------------------------
            # PROCESSING
            # ---------------------------------------------

            if (
                "[Merger]" in s
                or "[ExtractAudio]" in s
                or "Merging formats" in s
            ):

                t["status"] = "processing"

        rc = await p.wait()

        # -------------------------------------------------
        # FIRST ATTEMPT FAILED
        # -------------------------------------------------

        if rc != 0:

            # Удаляем результаты неудачной попытки

            for f in work.iterdir():

                if f.is_file():

                    try:
                        f.unlink()
                    except Exception:
                        pass

            # ------------------------------------------------
            # FALLBACK: TV CLIENT
            # ------------------------------------------------

            fallback_cmd = [
                "yt-dlp",

                *YT_BASE_ARGS,
                *YT_FALLBACK_ARGS,

                "--newline",
                "--progress",

                *fmt,

                "-o",
                output,

                x.url,
            ]

            if ffmpeg:
                fallback_cmd.extend([
                    "--ffmpeg-location",
                    ffmpeg,
                ])

            t["status"] = "retrying"
            t["percent"] = 0
            t["speed"] = ""
            t["eta"] = ""

            p = await asyncio.create_subprocess_exec(
                *fallback_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            while True:

                line = await p.stdout.readline()

                if not line:
                    break

                s = line.decode(
                    "utf-8",
                    "replace"
                ).strip()

                m = re.search(
                    r"\[download\]\s+([\d.]+)%.*?at\s+(.+?)\s+ETA\s+(.+)",
                    s
                )

                if m:

                    try:
                        t["percent"] = float(
                            m.group(1)
                        )
                    except Exception:
                        pass

                    t["speed"] = m.group(2)
                    t["eta"] = m.group(3)

                if "Destination:" in s:

                    filename = s.split(
                        "Destination:",
                        1
                    )[-1].strip()

                    t["filename"] = Path(
                        filename
                    ).name

                if (
                    "[Merger]" in s
                    or "[ExtractAudio]" in s
                    or "Merging formats" in s
                ):

                    t["status"] = "processing"

            rc = await p.wait()

        # -------------------------------------------------
        # FINAL ERROR
        # -------------------------------------------------

        if rc != 0:

            raise RuntimeError(
                "YouTube не разрешил загрузку "
                "этого видео через доступные клиенты."
            )

        # -------------------------------------------------
        # FIND FILE
        # -------------------------------------------------

        candidates = [
            p for p in work.iterdir()
            if p.is_file()
        ]

        if not candidates:

            raise RuntimeError(
                "Файл не найден после загрузки."
            )

        f = max(
            candidates,
            key=lambda p: p.stat().st_mtime
        )

        # -------------------------------------------------
        # DONE
        # -------------------------------------------------

        t.update({
            "status": "done",
            "percent": 100,
            "filename": f.name,
            "file": str(f),
        })

    except Exception as e:

        t.update({
            "status": "error",
            "error": str(e),
        })


# =========================================================
# PROGRESS
# =========================================================

@app.get("/api/progress/{tid}")
def progress(tid: str):

    if tid not in tasks:

        raise HTTPException(
            404,
            "Задача не найдена"
        )

    return tasks[tid]


# =========================================================
# FILE
# =========================================================

@app.get("/api/file/{tid}")
def file(tid: str):

    t = tasks.get(tid)

    if (
        not t
        or t["status"] != "done"
        or not t["file"]
    ):

        raise HTTPException(
            404,
            "Файл ещё не готов"
        )

    f = Path(t["file"])

    if not f.exists():

        raise HTTPException(
            404,
            "Файл удалён"
        )

    return FileResponse(
        f,
        filename=f.name,
        media_type="application/octet-stream"
    )


# =========================================================
# PRODUCTION
# =========================================================

# Render:
# uvicorn server:app --host 0.0.0.0 --port 8000