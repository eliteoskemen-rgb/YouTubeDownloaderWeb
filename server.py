import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="YouTube Downloader")

tasks = {}


# =========================================================
# PUBLIC PIPED INSTANCES
# =========================================================

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.syncpundit.io",
]


# =========================================================
# PUBLIC INVIDIOUS INSTANCES
# =========================================================

INVIDIOUS_INSTANCES = [
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
]


class Link(BaseModel):
    url: str


class Download(BaseModel):
    url: str
    quality: str = "best"
    mode: str = "video"


# =========================================================
# URL / VIDEO ID
# =========================================================

def get_video_id(url: str):
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()

    if host in {"youtu.be", "www.youtu.be"}:
        value = parsed.path.strip("/").split("/")[0]
        if value:
            return value

    if "youtube.com" in host:
        query = parse_qs(parsed.query)

        if query.get("v"):
            return query["v"][0]

        parts = [x for x in parsed.path.split("/") if x]

        if len(parts) >= 2 and parts[0] in {
            "shorts",
            "embed",
            "live",
        }:
            return parts[1]

    match = re.search(
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)"
        r"([A-Za-z0-9_-]{6,})",
        url,
    )

    return match.group(1) if match else None


def normalize_url(url: str):
    video_id = get_video_id(url)

    if not video_id:
        raise ValueError(
            "Не удалось определить YouTube video ID."
        )

    return (
        "https://www.youtube.com/watch?v="
        + video_id
    )


# =========================================================
# HTTP
# =========================================================

async def get_json(url: str):
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
    ) as client:

        response = await client.get(url)

        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}"
            )

        return response.json()


# =========================================================
# PIPED
# =========================================================

async def piped_streams(video_id: str):
    errors = []

    for base in PIPED_INSTANCES:

        try:
            data = await get_json(
                f"{base}/streams/{video_id}"
            )

            if data and (
                data.get("videoStreams")
                or data.get("audioStreams")
            ):
                return data, base

        except Exception as exc:
            errors.append(
                f"{base}: {exc}"
            )

    raise RuntimeError(
        "Все Piped-инстансы недоступны.\n"
        + "\n".join(errors)
    )


# =========================================================
# INVIDIOUS
# =========================================================

async def invidious_video(video_id: str):
    errors = []

    for base in INVIDIOUS_INSTANCES:

        try:
            data = await get_json(
                f"{base}/api/v1/videos/{video_id}"
            )

            if data and (
                data.get("adaptiveFormats")
                or data.get("formatStreams")
            ):
                return data, base

        except Exception as exc:
            errors.append(
                f"{base}: {exc}"
            )

    raise RuntimeError(
        "Все Invidious-инстансы недоступны.\n"
        + "\n".join(errors)
    )


# =========================================================
# QUALITY HELPERS
# =========================================================

def quality_number(value):
    match = re.search(
        r"(\d+)",
        str(value or "")
    )

    return int(match.group(1)) if match else 0


def desired_height(quality: str):
    if quality == "best":
        return 4320

    return max(
        144,
        min(
            4320,
            quality_number(quality),
        ),
    )


def choose_piped(video_streams, height):
    candidates = []

    for stream in video_streams or []:

        h = int(
            stream.get("height") or 0
        )

        if h <= 0 or h > height:
            continue

        url = stream.get("url")

        if not url:
            continue

        candidates.append(stream)

    if not candidates:
        return None

    # Highest resolution first.
    candidates.sort(
        key=lambda x: (
            int(x.get("height") or 0),
            1 if not x.get("videoOnly") else 0,
            int(x.get("bitrate") or 0),
        ),
        reverse=True,
    )

    return candidates[0]


def choose_audio(video):
    streams = video.get("audioStreams") or []

    candidates = [
        x
        for x in streams
        if x.get("url")
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            int(x.get("bitrate") or 0)
        ),
        reverse=True,
    )

    return candidates[0]


def choose_invidious_video(formats, height):
    candidates = []

    for fmt in formats or []:

        resolution = quality_number(
            fmt.get("height")
            or fmt.get("resolution")
            or fmt.get("qualityLabel")
        )

        if resolution <= 0 or resolution > height:
            continue

        if not fmt.get("url"):
            continue

        video_codec = (
            fmt.get("type", "")
            .lower()
        )

        if "video" not in video_codec:
            continue

        candidates.append(
            (
                resolution,
                fmt
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return candidates[0][1]


def choose_invidious_audio(formats):
    candidates = []

    for fmt in formats or []:

        if not fmt.get("url"):
            continue

        if "audio" not in (
            fmt.get("type", "")
            .lower()
        ):
            continue

        candidates.append(fmt)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: int(
            re.sub(
                r"\D",
                "",
                str(
                    x.get("bitrate")
                    or "0"
                )
            ) or 0
        ),
        reverse=True,
    )

    return candidates[0]


# =========================================================
# ROOT / HEALTH
# =========================================================

@app.get("/")
async def root():
    return FileResponse(
        BASE / "index.html"
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engine": "Piped + Invidious",
        "piped_instances": len(
            PIPED_INSTANCES
        ),
        "invidious_instances": len(
            INVIDIOUS_INSTANCES
        ),
    }


# =========================================================
# INFO
# =========================================================

@app.post("/api/info")
async def info(request: Link):

    try:
        url = normalize_url(
            request.url
        )

    except ValueError as exc:
        raise HTTPException(
            400,
            str(exc),
        )

    video_id = get_video_id(url)

    # -----------------------------------------------------
    # PIPED
    # -----------------------------------------------------

    try:

        data, source = await piped_streams(
            video_id
        )

        video_streams = (
            data.get("videoStreams")
            or []
        )

        qualities = {}

        for stream in video_streams:

            height = int(
                stream.get("height") or 0
            )

            if height <= 0:
                continue

            old = qualities.get(
                height
            )

            if old is None:
                qualities[height] = stream
                continue

            old_bitrate = int(
                old.get("bitrate") or 0
            )

            new_bitrate = int(
                stream.get("bitrate") or 0
            )

            if new_bitrate > old_bitrate:
                qualities[height] = stream

        format_list = []

        for height in sorted(
            qualities,
            reverse=True,
        ):

            stream = qualities[height]

            format_list.append({
                "quality": f"{height}p",
                "height": height,
                "filesize": None,
                "file_size_str": "",
            })

        return {
            "success": True,
            "title": data.get(
                "title",
                "YouTube video",
            ),
            "thumbnail": data.get(
                "thumbnailUrl",
                f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            ),
            "duration": data.get(
                "duration"
            ),
            "duration_str": "",
            "uploader": data.get(
                "uploader",
                "",
            ),
            "formats": format_list,
            "audioFormat": "mp3",
            "source": (
                "piped"
            ),
        }

    except Exception as piped_error:

        # -------------------------------------------------
        # FALLBACK INVIDIOUS
        # -------------------------------------------------

        try:

            data, source = (
                await invidious_video(
                    video_id
                )
            )

            formats = []

            heights = set()

            for fmt in data.get(
                "adaptiveFormats",
                [],
            ):

                height = quality_number(
                    fmt.get("height")
                    or fmt.get(
                        "resolution"
                    )
                    or fmt.get(
                        "qualityLabel"
                    )
                )

                if height > 0:
                    heights.add(height)

            for height in sorted(
                heights,
                reverse=True,
            ):

                formats.append({
                    "quality": f"{height}p",
                    "height": height,
                    "filesize": None,
                    "file_size_str": "",
                })

            thumb = ""

            thumbs = data.get(
                "videoThumbnails"
            ) or []

            if thumbs:
                thumb = max(
                    thumbs,
                    key=lambda x: (
                        x.get("width")
                        or 0
                    ),
                ).get(
                    "url",
                    "",
                )

            return {
                "success": True,
                "title": data.get(
                    "title",
                    "YouTube video",
                ),
                "thumbnail": (
                    thumb
                    or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
                ),
                "duration": data.get(
                    "lengthSeconds"
                ),
                "duration_str": "",
                "uploader": data.get(
                    "author",
                    "",
                ),
                "formats": formats,
                "audioFormat": "mp3",
                "source": (
                    "invidious"
                ),
            }

        except Exception as inv_error:

            raise HTTPException(
                502,
                "Piped и Invidious не смогли "
                "получить видео.\n\n"
                f"Piped: {piped_error}\n\n"
                f"Invidious: {inv_error}",
            )


# =========================================================
# DOWNLOAD
# =========================================================

@app.post("/api/download")
async def download(request: Download):

    try:
        url = normalize_url(
            request.url
        )

    except ValueError as exc:
        raise HTTPException(
            400,
            str(exc),
        )

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
        download_worker(
            task_id,
            url,
            request.quality,
            request.mode,
        )
    )

    return {
        "id": task_id
    }


# =========================================================
# WORKER
# =========================================================

async def download_worker(
    task_id,
    url,
    quality,
    mode,
):

    task = tasks[task_id]

    work = DOWNLOADS / task_id

    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        video_id = get_video_id(url)

        height = desired_height(
            quality
        )

        task["status"] = "downloading"
        task["message"] = (
            "Ищу рабочий поток..."
        )

        video_url = None
        audio_url = None
        title = "youtube-video"

        # -------------------------------------------------
        # Try Piped
        # -------------------------------------------------

        try:

            data, source = (
                await piped_streams(
                    video_id
                )
            )

            title = (
                data.get("title")
                or title
            )

            if mode == "audio":

                audio = choose_audio(
                    data
                )

                if not audio:
                    raise RuntimeError(
                        "Piped audio stream not found."
                    )

                audio_url = audio["url"]

            else:

                video = choose_piped(
                    data.get(
                        "videoStreams",
                        []
                    ),
                    height,
                )

                audio = choose_audio(
                    data
                )

                if not video:
                    raise RuntimeError(
                        "Piped video stream not found."
                    )

                if not audio:
                    raise RuntimeError(
                        "Piped audio stream not found."
                    )

                video_url = video["url"]
                audio_url = audio["url"]

        except Exception:

            # ------------------------------------------------
            # Invidious fallback
            # ------------------------------------------------

            data, source = (
                await invidious_video(
                    video_id
                )
            )

            title = (
                data.get("title")
                or title
            )

            formats = data.get(
                "adaptiveFormats",
                []
            )

            if mode == "audio":

                audio = choose_invidious_audio(
                    formats
                )

                if not audio:
                    raise RuntimeError(
                        "Invidious audio stream not found."
                    )

                audio_url = audio["url"]

            else:

                video = choose_invidious_video(
                    formats,
                    height,
                )

                audio = choose_invidious_audio(
                    formats
                )

                if not video:
                    raise RuntimeError(
                        "Invidious video stream not found."
                    )

                if not audio:
                    raise RuntimeError(
                        "Invidious audio stream not found."
                    )

                video_url = video["url"]
                audio_url = audio["url"]

        # -------------------------------------------------
        # Safe filename
        # -------------------------------------------------

        safe_title = re.sub(
            r'[\\/:*?"<>|]+',
            "_",
            title,
        )[:120].strip()

        if not safe_title:
            safe_title = "youtube-video"

        output = work / (
            f"{safe_title}.mp3"
            if mode == "audio"
            else f"{safe_title}.mp4"
        )

        # -------------------------------------------------
        # FFmpeg
        # -------------------------------------------------

        if mode == "audio":

            command = [
                "ffmpeg",
                "-y",
                "-i",
                audio_url,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(output),
            ]

        else:

            command = [
                "ffmpeg",
                "-y",

                "-i",
                video_url,

                "-i",
                audio_url,

                "-map",
                "0:v:0",

                "-map",
                "1:a:0",

                "-c:v",
                "copy",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-shortest",

                "-movflags",
                "+faststart",

                str(output),
            ]

        task["message"] = (
            "Собираю файл..."
        )

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,
        )

        while True:

            line = (
                await process.stdout.readline()
            )

            if not line:
                break

            text = line.decode(
                "utf-8",
                "replace",
            ).strip()

            task["log"] = text[-1500:]

            # Try to parse ffmpeg progress.
            match = re.search(
                r"time=(\d+):(\d+):([\d.]+)",
                text,
            )

            if match:
                task["percent"] = min(
                    99,
                    task["percent"] + 1,
                )

        code = await process.wait()

        if code != 0:

            raise RuntimeError(
                task["log"]
                or "FFmpeg завершился с ошибкой."
            )

        if not output.exists():
            raise RuntimeError(
                "Готовый файл не найден."
            )

        task.update({
            "status": "done",
            "percent": 100,
            "filename": output.name,
            "file": str(output),
            "message": "Готово",
        })

    except Exception as exc:

        task.update({
            "status": "error",
            "percent": 0,
            "error": str(exc),
            "message": "Ошибка",
        })


# =========================================================
# PROGRESS
# =========================================================

@app.get("/api/progress/{task_id}")
async def progress(task_id: str):

    if task_id not in tasks:
        raise HTTPException(
            404,
            "Задача не найдена.",
        )

    task = tasks[task_id]

    result = {
        "success": True,
        **task,
    }

    if task["status"] == "done":
        result["download_url"] = (
            f"/api/file/{task_id}"
        )

    return result


# =========================================================
# FILE
# =========================================================

@app.get("/api/file/{task_id}")
async def file(task_id: str):

    task = tasks.get(task_id)

    if not task:
        raise HTTPException(
            404,
            "Задача не найдена.",
        )

    if task["status"] != "done":
        raise HTTPException(
            400,
            "Файл ещё не готов.",
        )

    path = Path(
        task["file"]
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Файл больше не существует.",
        )

    return FileResponse(
        path,
        filename=task["filename"],
        media_type="application/octet-stream",
    )