import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"

DOWNLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="YouTube Downloader")

tasks = {}

# bgutil server is started by Docker/entrypoint on localhost:4416
YT_ARGS = [
    "--no-playlist",
    "--js-runtimes",
    "deno",
    "--extractor-args",
    "youtube:player-client=mweb",
    "--extractor-args",
    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
]

URL_RE = re.compile(r"^https?://", re.I)


class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "youtube-downloader",
        "engine": "yt-dlp + bgutil",
    }


def make_task():
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


async def run_info(url: str):
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
        raise RuntimeError(
            err.decode("utf-8", "replace")[-6000:]
        )

    return json.loads(out)


@app.post("/api/info")
async def info(x: Link):

    url = x.url.strip()

    if not URL_RE.match(url):
        raise HTTPException(400, "Неверная ссылка")

    try:
        data = await run_info(url)

    except Exception as e:
        raise HTTPException(
            502,
            str(e),
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

    tid = make_task()

    asyncio.create_task(
        run_download(
            tid,
            x,
        )
    )

    return {
        "id": tid,
    }


async def run_download(tid: str, x: Download):

    task = tasks[tid]

    work = DOWNLOADS / tid
    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

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

                except ValueError:
                    height = 1080

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

        output = str(
            work / "%(title).150s.%(ext)s"
        )

        cmd = [
            "yt-dlp",
            *YT_ARGS,
            "--newline",
            "--progress",
            "--ffmpeg-location",
            shutil.which("ffmpeg") or "",
            *fmt,
            "-o",
            output,
            url,
        ]

        cmd = [
            c for c in cmd
            if c != ""
        ]

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

            task["filename"] = text

            match = re.search(
                r"\[download\]\s+([\d.]+)%.*?"
                r"at\s+(.+?)\s+ETA\s+(.+)",
                text,
            )

            if match:

                task["percent"] = float(
                    match.group(1)
                )

                task["speed"] = match.group(2)
                task["eta"] = match.group(3)
                task["status"] = "downloading"

            if (
                "Merging formats" in text
                or "[Merger]" in text
                or "Destination:" in text
            ):
                task["status"] = "processing"

            if (
                "ERROR:" in text
                or "error:" in text
            ):
                task["error"] = text[-3000:]

        rc = await process.wait()

        if rc != 0:

            raise RuntimeError(
                task["error"]
                or "yt-dlp не смог скачать файл"
            )

        candidates = [
            p
            for p in work.iterdir()
            if p.is_file()
        ]

        if not candidates:

            raise RuntimeError(
                "После загрузки файл не найден"
            )

        file_path = max(
            candidates,
            key=lambda p: p.stat().st_mtime
        )

        task.update(
            {
                "status": "done",
                "percent": 100,
                "filename": file_path.name,
                "file": str(file_path),
            }
        )

    except Exception as e:

        task.update(
            {
                "status": "error",
                "error": str(e),
            }
        )


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

    task = tasks.get(tid)

    if (
        not task
        or task["status"] != "done"
        or not task["file"]
    ):
        raise HTTPException(
            404,
            "Файл ещё не готов"
        )

    f = Path(task["file"])

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