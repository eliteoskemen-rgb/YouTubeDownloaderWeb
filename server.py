import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(parents=True, exist_ok=True)

YTDLP = os.getenv("YTDLP_BIN", "yt-dlp")
BGUTIL_SCRIPT = os.getenv(
    "BGUTIL_SCRIPT",
    "/opt/bgutil/server/build/generate_once.js",
)

app = FastAPI(title="YouTube Downloader")
tasks = {}


class Link(BaseModel):
    url: str


class Download(Link):
    quality: str = "best"
    mode: str = "video"


def normalize_url(url: str) -> str:
    value = url.strip()
    p = urlparse(value)
    host = (p.hostname or "").lower()

    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"

    if host == "youtube.com" or host.endswith(".youtube.com"):
        q = parse_qs(p.query)
        if q.get("v"):
            return f"https://www.youtube.com/watch?v={q['v'][0]}"

        parts = [part for part in p.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return f"https://www.youtube.com/{parts[0]}/{parts[1]}"

    raise ValueError("Нужна корректная ссылка YouTube.")


def video_id(url: str):
    p = urlparse(url)
    host = (p.hostname or "").lower()

    if host == "youtu.be":
        return p.path.strip("/").split("/")[0]

    q = parse_qs(p.query)
    if q.get("v"):
        return q["v"][0]

    parts = [part for part in p.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1]

    return ""


def base_args():
    return [
        YTDLP,
        "--ignore-config",
        "--no-warnings",
        "--no-playlist",
        "--js-runtimes",
        "deno",
        "--extractor-args",
        f"youtube:player-client=mweb",
        "--extractor-args",
        f"youtubepot-bgutilscript:script_path={BGUTIL_SCRIPT}",
    ]


async def run_cmd(args, timeout=180):
    proc = await asyncio.create_subprocess_exec(
        *(base_args() + args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        output, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("yt-dlp превысил время ожидания.")

    return proc.returncode, output.decode("utf-8", "replace")


def unique_formats(data):
    result = {}

    for fmt in data.get("formats") or []:
        height = fmt.get("height")
        if not height or height < 144:
            continue

        ext = fmt.get("ext") or ""
        has_video = fmt.get("vcodec") not in (None, "none")
        has_audio = fmt.get("acodec") not in (None, "none")

        # Prefer video+audio first, then video-only.
        score = (
            100000 if has_audio else 0
        ) + height * 100 + (
            1 if ext == "mp4" else 0
        )

        item = result.get(height)
        if item is None or score > item["_score"]:
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            result[height] = {
                "quality": f"{height}p",
                "height": height,
                "ext": ext,
                "filesize": size,
                "_score": score,
            }

    output = []
    for height, item in sorted(result.items(), reverse=True):
        item.pop("_score", None)
        if item["filesize"]:
            mb = item["filesize"] / 1024 / 1024
            item["file_size_str"] = f"{mb:.1f} MB"
        else:
            item["file_size_str"] = ""
        output.append(item)

    return output


@app.get("/")
async def root():
    return FileResponse(BASE / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engine": "yt-dlp + bgutil script provider",
        "yt_dlp": shutil.which(YTDLP) or YTDLP,
        "bgutil_script": BGUTIL_SCRIPT,
        "bgutil_script_exists": Path(BGUTIL_SCRIPT).exists(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "deno": bool(shutil.which("deno")),
    }


@app.post("/api/info")
async def info(request: Link):
    try:
        url = normalize_url(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rc, text = await run_cmd(
        [
            "--dump-single-json",
            "--skip-download",
            url,
        ],
        timeout=120,
    )

    if rc != 0:
        raise HTTPException(
            502,
            text[-8000:] or "yt-dlp не получил информацию.",
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(
            502,
            "yt-dlp вернул некорректный JSON.",
        )

    vid = video_id(url)

    return {
        "success": True,
        "id": vid,
        "title": data.get("title") or "YouTube video",
        "thumbnail": data.get("thumbnail")
        or f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        "duration": data.get("duration"),
        "uploader": data.get("uploader")
        or data.get("channel")
        or "",
        "formats": unique_formats(data),
        "audioFormat": "mp3",
    }


@app.post("/api/download")
async def download(request: Download):
    try:
        url = normalize_url(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    task_id = uuid.uuid4().hex
    tasks[task_id] = {
        "status": "queued",
        "percent": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "error": "",
        "file": "",
        "log": "",
    }

    asyncio.create_task(
        worker(
            task_id,
            url,
            request.quality,
            request.mode,
        )
    )

    return {"id": task_id}


async def worker(task_id, url, quality, mode):
    task = tasks[task_id]
    work = DOWNLOADS / task_id
    work.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "audio":
            output = work / "%(title).150s.%(ext)s"
            args = [
                "--newline",
                "--progress",
                "-x",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "192K",
                "-o",
                str(output),
                url,
            ]
        else:
            try:
                maximum = int(str(quality).rstrip("p"))
            except ValueError:
                maximum = 4320

            maximum = max(144, min(4320, maximum))

            output = work / "%(title).150s.%(ext)s"
            fmt = (
                f"bestvideo[height<={maximum}]"
                f"+bestaudio/"
                f"best[height<={maximum}]"
                f"/best"
            )

            args = [
                "--newline",
                "--progress",
                "-f",
                fmt,
                "--merge-output-format",
                "mp4",
                "-o",
                str(output),
                url,
            ]

        task["status"] = "downloading"

        proc = await asyncio.create_subprocess_exec(
            *(base_args() + args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", "replace").strip()
            task["log"] = text[-1500:]

            match = re.search(
                r"\[download\]\s+([\d.]+)%",
                text,
            )
            if match:
                task["percent"] = float(match.group(1))

            speed = re.search(r"\bat\s+(.+?)\s+ETA\s+", text)
            eta = re.search(r"\bETA\s+(.+)$", text)

            if speed:
                task["speed"] = speed.group(1)
            if eta:
                task["eta"] = eta.group(1)

            if "Destination:" in text:
                task["status"] = "processing"
            elif "Merging formats" in text:
                task["status"] = "processing"

        rc = await proc.wait()

        if rc != 0:
            raise RuntimeError(
                task["log"] or "yt-dlp завершился с ошибкой."
            )

        files = [
            p for p in work.iterdir()
            if p.is_file()
        ]

        if not files:
            raise RuntimeError("Файл после загрузки не найден.")

        result = max(
            files,
            key=lambda p: p.stat().st_mtime,
        )

        task.update({
            "status": "done",
            "percent": 100,
            "filename": result.name,
            "file": str(result),
        })

    except Exception as exc:
        task.update({
            "status": "error",
            "error": str(exc),
        })


@app.get("/api/progress/{task_id}")
async def progress(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена.")

    response = {"success": True, **task}

    if task["status"] == "done":
        response["download_url"] = f"/api/file/{task_id}"

    return response


@app.get("/api/file/{task_id}")
async def get_file(task_id: str):
    task = tasks.get(task_id)

    if not task:
        raise HTTPException(404, "Задача не найдена.")

    if task["status"] != "done":
        raise HTTPException(400, "Файл ещё не готов.")

    path = Path(task["file"])

    if not path.exists():
        raise HTTPException(404, "Файл больше не существует.")

    return FileResponse(
        path,
        filename=task["filename"],
        media_type="application/octet-stream",
    )
