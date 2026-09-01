import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE = Path(__file__).resolve().parent

DOWNLOADS = BASE / "downloads"

# Render Secret File — только для чтения
COOKIES_SOURCE = Path("/etc/secrets/www.youtube.com_cookies.txt")

# Временная папка, куда можно писать
TEMP_DIR = Path(tempfile.gettempdir()) / "youtube_downloader"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOADS.mkdir(parents=True, exist_ok=True)


app = FastAPI(title="YouTube Downloader Online")

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static"
)


tasks = {}

URL_RE = re.compile(r"^https?://", re.I)


class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


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


def prepare_cookies(tid: str):
    """
    Render Secret File находится в /etc/secrets и доступен только для чтения.
    yt-dlp иногда пытается обновить cookies.

    Поэтому делаем копию в /tmp, где файл доступен для записи.
    """

    if not COOKIES_SOURCE.exists():
        return None

    cookie_file = TEMP_DIR / f"cookies_{tid}.txt"

    try:
        shutil.copy2(COOKIES_SOURCE, cookie_file)
        return cookie_file
    except Exception as e:
        raise RuntimeError(
            f"Не удалось подготовить cookies: {e}"
        )


def yt_args(tid: str):
    """
    Возвращает аргументы yt-dlp.
    """

    args = [
        "--js-runtimes",
        "deno",
    ]

    cookie_file = prepare_cookies(tid)

    if cookie_file:
        args.extend([
            "--cookies",
            str(cookie_file),
        ])

    return args


@app.post("/api/info")
async def info(x: Link):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

    temp_id = uuid.uuid4().hex

    try:
        args = yt_args(temp_id)

        cmd = [
            "yt-dlp",
            *args,
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            url,
        ]

        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out, err = await p.communicate()

        if p.returncode != 0:
            error = err.decode(
                "utf-8",
                "replace"
            )

            raise HTTPException(
                400,
                error[-2000:]
            )

        try:
            data = json.loads(out)
        except Exception:
            raise HTTPException(
                500,
                "Не удалось получить информацию о видео"
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

    finally:
        cookie_file = TEMP_DIR / f"cookies_{temp_id}.txt"

        try:
            cookie_file.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/download")
async def download(x: Download):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

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

    cookie_file = None

    try:

        # -------------------------------------------------
        # COOKIES
        # -------------------------------------------------

        cookie_file = prepare_cookies(tid)

        yt_args_list = [
            "--js-runtimes",
            "deno",
        ]

        if cookie_file:
            yt_args_list.extend([
                "--cookies",
                str(cookie_file),
            ])

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
                    h = int(x.quality)
                except ValueError:
                    h = 720

                fmt = [
                    "-f",
                    (
                        f"bestvideo[height<={h}]"
                        f"+bestaudio/best"
                        f"[height<={h}]"
                        f"/best[height<={h}]"
                        f"/best"
                    ),
                    "--merge-output-format",
                    "mp4",
                ]

        # -------------------------------------------------
        # OUTPUT
        # -------------------------------------------------

        out = str(
            work / "%(title)s.%(ext)s"
        )

        ffmpeg = shutil.which("ffmpeg")

        cmd = [
            "yt-dlp",
            *yt_args_list,

            "--newline",
            "--progress",
            "--no-playlist",

            "--no-warnings",

            *fmt,

            "-o",
            out,

            x.url,
        ]

        if ffmpeg:
            cmd.extend([
                "--ffmpeg-location",
                ffmpeg,
            ])

        # -------------------------------------------------
        # START YT-DLP
        # -------------------------------------------------

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
                "replace"
            ).strip()

            # -------------------------------------------------
            # PROGRESS
            # -------------------------------------------------

            m = re.search(
                r"\[download\]\s+"
                r"([\d.]+)%"
                r".*?"
                r"at\s+(.+?)"
                r"\s+ETA\s+(.+)",
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

            # -------------------------------------------------
            # FILENAME
            # -------------------------------------------------

            if "[download]" in s and "%" in s:
                t["filename"] = (
                    s.split("]")[-1].strip()
                )

            # -------------------------------------------------
            # PROCESSING
            # -------------------------------------------------

            if (
                "[Merger]" in s
                or "Destination:" in s
                or "[ExtractAudio]" in s
            ):
                t["status"] = "processing"

        rc = await p.wait()

        # -------------------------------------------------
        # ERROR
        # -------------------------------------------------

        if rc != 0:

            raise RuntimeError(
                "yt-dlp не смог скачать файл. "
                "Проверь cookies YouTube и ссылку."
            )

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

        f = max(
            candidates,
            key=lambda p: p.stat().st_mtime
        )

        # -------------------------------------------------
        # DONE
        # -------------------------------------------------

        t.update(
            status="done",
            percent=100,
            filename=f.name,
            file=str(f),
        )

    except Exception as e:

        t.update(
            status="error",
            error=str(e),
        )

    finally:

        # -------------------------------------------------
        # DELETE TEMP COOKIES
        # -------------------------------------------------

        if cookie_file:

            try:
                cookie_file.unlink(
                    missing_ok=True
                )
            except Exception:
                pass


@app.get("/api/progress/{tid}")
def progress(tid: str):

    if tid not in tasks:
        raise HTTPException(
            404,
            "Задача не найдена"
        )

    return tasks[tid]


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
        media_type="application/octet-stream",
    )


# Production:
#
# uvicorn server:app --host 0.0.0.0 --port 8000