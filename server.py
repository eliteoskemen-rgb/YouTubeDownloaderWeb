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


# =========================================================
# PATHS
# =========================================================

BASE = Path(__file__).resolve().parent

DOWNLOADS = BASE / "downloads"

# Render Secret File
COOKIES_SOURCE = Path(
    "/etc/secrets/www.youtube.com_cookies.txt"
)

# Writable temporary directory
TEMP_DIR = Path(tempfile.gettempdir()) / "youtube_downloader"

DOWNLOADS.mkdir(
    parents=True,
    exist_ok=True
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# =========================================================
# APP
# =========================================================

app = FastAPI(
    title="YouTube Downloader Online"
)

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static"
)


# =========================================================
# STORAGE
# =========================================================

tasks = {}

URL_RE = re.compile(
    r"^https?://",
    re.I
)


# =========================================================
# MODELS
# =========================================================

class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


# =========================================================
# BASIC ROUTES
# =========================================================

@app.get("/")
def index():
    return FileResponse(
        BASE / "index.html"
    )


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
# COOKIES
# =========================================================

def prepare_cookies(tid: str):

    """
    Render Secret Files are read-only.

    We therefore copy cookies into /tmp.
    yt-dlp can work with the writable copy.
    """

    if not COOKIES_SOURCE.exists():
        return None

    cookie_file = (
        TEMP_DIR /
        f"cookies_{tid}.txt"
    )

    try:

        shutil.copyfile(
            COOKIES_SOURCE,
            cookie_file
        )

        return cookie_file

    except Exception as e:

        raise RuntimeError(
            f"Не удалось подготовить cookies: {e}"
        )


def remove_cookies(cookie_file):

    if not cookie_file:
        return

    try:
        cookie_file.unlink(
            missing_ok=True
        )
    except Exception:
        pass


# =========================================================
# YT-DLP BASE ARGUMENTS
# =========================================================

def base_yt_args():

    return [
        "--js-runtimes",
        "deno",

        # Keep EJS scripts up to date.
        "--remote-components",
        "ejs:github",

        "--no-playlist",
        "--no-warnings",
    ]


# =========================================================
# PUBLIC YOUTUBE CLIENTS
# =========================================================

def public_client_args():

    """
    Clients that can be useful for public videos.

    yt-dlp itself decides which formats are actually
    available for the selected client.
    """

    return [
        "--extractor-args",
        (
            "youtube:"
            "player_client=android_vr,web_embedded,web_safari"
        ),
    ]


# =========================================================
# RUN YT-DLP
# =========================================================

async def execute_yt_dlp(
    cmd,
    timeout=300
):

    try:

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    except FileNotFoundError as e:

        raise RuntimeError(
            "yt-dlp или Deno не найден на сервере."
        ) from e

    output = []

    try:

        while True:

            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=timeout
            )

            if not line:
                break

            text = line.decode(
                "utf-8",
                "replace"
            ).strip()

            if text:
                output.append(text)

        return_code = await process.wait()

    except asyncio.TimeoutError:

        try:
            process.kill()
        except Exception:
            pass

        await process.wait()

        raise RuntimeError(
            "YouTube слишком долго не отвечает."
        )

    text = "\n".join(output)

    return return_code, text


# =========================================================
# ERROR CLEANUP
# =========================================================

def clean_youtube_error(text):

    text = text.strip()

    if not text:
        return "YouTube не вернул описание ошибки."

    lower = text.lower()

    if (
        "sign in to confirm" in lower
        or "not a bot" in lower
    ):
        return (
            "YouTube требует дополнительную проверку "
            "для этого запроса. "
            "Повторная попытка с cookies будет выполнена "
            "автоматически."
        )

    if (
        "cookies are no longer valid" in lower
        or "cookies have been rotated" in lower
    ):
        return (
            "Cookies YouTube устарели. "
            "Обнови www.youtube.com_cookies.txt "
            "в Render → Environment → Secret Files."
        )

    if (
        "video unavailable" in lower
        or "this video is unavailable" in lower
    ):
        return (
            "Видео недоступно для загрузки "
            "или ограничено владельцем."
        )

    if "private video" in lower:
        return "Видео является приватным."

    if "age-restricted" in lower:
        return (
            "Видео имеет возрастное ограничение. "
            "Для него могут потребоваться действующие cookies."
        )

    if "http error 403" in lower:
        return (
            "YouTube отклонил запрос (HTTP 403). "
            "Попробуйте ещё раз или обновите yt-dlp/cookies."
        )

    if "http error 429" in lower:
        return (
            "YouTube временно ограничил количество запросов."
        )

    # Return only the useful tail.
    lines = text.splitlines()

    useful = [
        line
        for line in lines
        if line.strip()
    ]

    return "\n".join(
        useful[-8:]
    )[:2500]


# =========================================================
# VIDEO INFO
# =========================================================

@app.post("/api/info")
async def info(x: Link):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            400,
            "Неверная ссылка"
        )

    temp_id = uuid.uuid4().hex

    cookie_file = None

    # -----------------------------------------------------
    # ATTEMPT 1
    # Without cookies
    # -----------------------------------------------------

    attempts = []

    attempts.append(
        (
            "public",
            base_yt_args()
            + public_client_args()
        )
    )

    # -----------------------------------------------------
    # ATTEMPT 2
    # With cookies
    # -----------------------------------------------------

    try:

        cookie_file = prepare_cookies(
            temp_id
        )

        if cookie_file:

            attempts.append(
                (
                    "cookies",
                    base_yt_args()
                    + [
                        "--cookies",
                        str(cookie_file),
                    ]
                )
            )

        last_error = ""

        for mode, args in attempts:

            cmd = [
                "yt-dlp",
                *args,

                "--dump-single-json",
                "--skip-download",

                url,
            ]

            return_code, output = (
                await execute_yt_dlp(
                    cmd,
                    timeout=120
                )
            )

            if return_code == 0:

                try:

                    data = json.loads(
                        output
                    )

                except Exception:

                    raise HTTPException(
                        500,
                        "YouTube вернул неожиданный ответ."
                    )

                return {
                    "title": data.get(
                        "title",
                        "Видео"
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
                        ""
                    ),
                }

            last_error = output

        raise HTTPException(
            400,
            clean_youtube_error(
                last_error
            )
        )

    finally:

        remove_cookies(
            cookie_file
        )


# =========================================================
# START DOWNLOAD
# =========================================================

@app.post("/api/download")
async def download(x: Download):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(
            400,
            "Неверная ссылка"
        )

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

async def run_download(
    tid,
    x
):

    t = tasks[tid]

    work = (
        DOWNLOADS /
        tid
    )

    work.mkdir(
        parents=True,
        exist_ok=True
    )

    cookie_file = None

    try:

        t["status"] = "starting"

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
                except Exception:
                    height = 720

                fmt = [
                    "-f",
                    (
                        f"bestvideo[height<={height}]"
                        "+bestaudio/"
                        f"best[height<={height}]/"
                        "best"
                    ),

                    "--merge-output-format",
                    "mp4",
                ]

        # -------------------------------------------------
        # OUTPUT
        # -------------------------------------------------

        output_template = str(
            work /
            "%(title)s.%(ext)s"
        )

        ffmpeg = shutil.which(
            "ffmpeg"
        )

        # -------------------------------------------------
        # BUILD ATTEMPTS
        # -------------------------------------------------

        attempts = []

        # ATTEMPT 1:
        # public video, without account cookies

        attempts.append(
            (
                "public",
                base_yt_args()
                + public_client_args()
            )
        )

        # ATTEMPT 2:
        # cookies

        cookie_file = prepare_cookies(
            tid
        )

        if cookie_file:

            attempts.append(
                (
                    "cookies",
                    base_yt_args()
                    + [
                        "--cookies",
                        str(cookie_file),
                    ]
                )
            )

        last_output = ""

        # -------------------------------------------------
        # TRY
        # -------------------------------------------------

        for attempt_number, (
            attempt_name,
            yt_args_list
        ) in enumerate(
            attempts,
            start=1
        ):

            t["status"] = (
                "downloading"
                if attempt_number == 1
                else "retrying"
            )

            # Clean old partial files.
            for old_file in work.iterdir():

                try:

                    if old_file.is_file():
                        old_file.unlink()

                except Exception:
                    pass

            cmd = [
                "yt-dlp",

                *yt_args_list,

                "--newline",
                "--progress",

                *fmt,

                "-o",
                output_template,

                x.url,
            ]

            if ffmpeg:

                cmd.extend([
                    "--ffmpeg-location",
                    ffmpeg,
                ])

            # -------------------------------------------------
            # START
            # -------------------------------------------------

            try:

                process = (
                    await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                )

            except FileNotFoundError:

                raise RuntimeError(
                    "yt-dlp не найден."
                )

            output_lines = []

            while True:

                line = (
                    await process.stdout.readline()
                )

                if not line:
                    break

                s = line.decode(
                    "utf-8",
                    "replace"
                ).strip()

                if not s:
                    continue

                output_lines.append(s)

                # ---------------------------------------------
                # PROGRESS
                # ---------------------------------------------

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

                # ---------------------------------------------
                # FILENAME
                # ---------------------------------------------

                if (
                    "[download]" in s
                    and "%" in s
                ):

                    t["filename"] = (
                        s.split("]")[-1].strip()
                    )

                # ---------------------------------------------
                # PROCESSING
                # ---------------------------------------------

                if (
                    "[Merger]" in s
                    or "[ExtractAudio]" in s
                    or "Destination:" in s
                ):

                    t["status"] = "processing"

            rc = await process.wait()

            last_output = "\n".join(
                output_lines
            )

            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

            if rc == 0:

                candidates = [
                    p
                    for p in work.iterdir()
                    if p.is_file()
                    and not p.name.endswith(".part")
                    and not p.name.endswith(".ytdl")
                ]

                if candidates:

                    f = max(
                        candidates,
                        key=lambda p:
                        p.stat().st_mtime
                    )

                    t.update(
                        status="done",
                        percent=100,
                        filename=f.name,
                        file=str(f),
                        error="",
                    )

                    return

            # -------------------------------------------------
            # FIRST ATTEMPT FAILED
            # -------------------------------------------------

            # If the public attempt failed,
            # automatically try cookies.

            if attempt_number < len(attempts):

                continue

            break

        # -----------------------------------------------------
        # ALL ATTEMPTS FAILED
        # -----------------------------------------------------

        raise RuntimeError(
            clean_youtube_error(
                last_output
            )
        )

    except Exception as e:

        t.update(
            status="error",
            error=str(e),
        )

    finally:

        remove_cookies(
            cookie_file
        )


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

    f = Path(
        t["file"]
    )

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


# =========================================================
# PRODUCTION
# =========================================================

# uvicorn server:app --host 0.0.0.0 --port 8000