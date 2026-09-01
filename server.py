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


BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"

# Render Secret File.
# Если cookies-файла нет — приложение всё равно сможет работать без него.
COOKIES = Path("/etc/secrets/www.youtube.com_cookies.txt")

DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="YouTube Downloader Online")

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static",
)

tasks = {}

URL_RE = re.compile(r"^https?://", re.I)


class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


def get_yt_args():
    """
    Общие параметры yt-dlp.
    Cookies используем только если файл реально существует
    и не пустой.
    """

    args = [
        "--js-runtimes",
        "deno",
        "--remote-components",
        "ejs:github",
    ]

    if COOKIES.exists() and COOKIES.stat().st_size > 0:
        args.extend([
            "--cookies",
            str(COOKIES),
        ])

    return args


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


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "yt_dlp": shutil.which("yt-dlp") or "",
        "ffmpeg": shutil.which("ffmpeg") or "",
        "deno": shutil.which("deno") or "",
        "cookies": COOKIES.exists() and COOKIES.stat().st_size > 0,
    }


@app.post("/api/info")
async def info(x: Link):
    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

    cmd = [
        "yt-dlp",
        *get_yt_args(),
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        url,
    ]

    try:
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out, err = await p.communicate()

    except Exception as e:
        raise HTTPException(
            500,
            f"Не удалось запустить yt-dlp: {e}",
        )

    if p.returncode != 0:
        error = err.decode("utf-8", "replace").strip()

        raise HTTPException(
            400,
            error[-3000:] or "yt-dlp не смог получить информацию о видео",
        )

    try:
        data = json.loads(out)
    except Exception:
        raise HTTPException(
            500,
            "yt-dlp вернул некорректный JSON",
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


@app.post("/api/download")
async def download(x: Download):
    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

    if x.mode not in ("video", "audio"):
        raise HTTPException(400, "Неверный тип загрузки")

    tid = safe_task()

    asyncio.create_task(
        run_download(tid, x)
    )

    return {
        "id": tid
    }


async def run_download(tid, x):
    t = tasks[tid]

    work = DOWNLOADS / tid
    work.mkdir(parents=True, exist_ok=True)

    try:

        # -----------------------------------------
        # AUDIO / MP3
        # -----------------------------------------

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

        # -----------------------------------------
        # VIDEO / MP4
        # -----------------------------------------

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

        # -----------------------------------------
        # OUTPUT
        # -----------------------------------------

        output = str(
            work / "%(title)s.%(ext)s"
        )

        ffmpeg = shutil.which("ffmpeg")

        cmd = [
            "yt-dlp",

            *get_yt_args(),

            "--newline",
            "--progress",
            "--no-playlist",

            "--no-warnings",

            "--output",
            output,
        ]

        if ffmpeg:
            cmd.extend([
                "--ffmpeg-location",
                ffmpeg,
            ])

        cmd.extend(fmt)

        cmd.append(x.url.strip())

        # -----------------------------------------
        # START PROCESS
        # -----------------------------------------

        t["status"] = "downloading"

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
                "replace",
            ).strip()

            if not s:
                continue

            # -------------------------------------
            # PROGRESS
            # -------------------------------------

            progress = re.search(
                r"\[download\]\s+([\d.]+)%",
                s,
            )

            if progress:
                try:
                    t["percent"] = float(
                        progress.group(1)
                    )
                except Exception:
                    pass

            # Speed
            speed = re.search(
                r"at\s+(.+?)\s+ETA",
                s,
            )

            if speed:
                t["speed"] = speed.group(1)

            # ETA
            eta = re.search(
                r"ETA\s+(.+)",
                s,
            )

            if eta:
                t["eta"] = eta.group(1)

            # Destination
            if "Destination:" in s:

                filename = s.split(
                    "Destination:",
                    1,
                )[1].strip()

                t["filename"] = Path(
                    filename
                ).name

            # Merger
            if "[Merger]" in s:

                t["status"] = "processing"

            # Postprocessing
            if "Deleting original file" in s:

                t["status"] = "processing"

        rc = await p.wait()

        # -----------------------------------------
        # ERROR
        # -----------------------------------------

        if rc != 0:

            raise RuntimeError(
                "yt-dlp завершился с ошибкой. "
                "Проверьте подробности в логах Render."
            )

        # -----------------------------------------
        # FIND RESULT FILE
        # -----------------------------------------

        candidates = [
            p
            for p in work.rglob("*")
            if p.is_file()
        ]

        if not candidates:

            raise RuntimeError(
                "Файл не найден после загрузки."
            )

        # Берём самый свежий файл
        result = max(
            candidates,
            key=lambda p: p.stat().st_mtime,
        )

        # -----------------------------------------
        # SUCCESS
        # -----------------------------------------

        t.update({
            "status": "done",
            "percent": 100,
            "filename": result.name,
            "file": str(result),
            "error": "",
        })

    except Exception as e:

        t.update({
            "status": "error",
            "error": str(e),
        })


@app.get("/api/progress/{tid}")
def progress(tid: str):

    if tid not in tasks:
        raise HTTPException(
            404,
            "Задача не найдена",
        )

    return tasks[tid]


@app.get("/api/file/{tid}")
def file(tid: str):

    task = tasks.get(tid)

    if not task:
        raise HTTPException(
            404,
            "Задача не найдена",
        )

    if task["status"] != "done":
        raise HTTPException(
            404,
            "Файл ещё не готов",
        )

    if not task["file"]:
        raise HTTPException(
            404,
            "Файл не найден",
        )

    f = Path(task["file"])

    if not f.exists():
        raise HTTPException(
            404,
            "Файл удалён",
        )

    return FileResponse(
        f,
        filename=f.name,
        media_type="application/octet-stream",
    )


# Production:
#
# uvicorn server:app --host 0.0.0.0 --port 8000