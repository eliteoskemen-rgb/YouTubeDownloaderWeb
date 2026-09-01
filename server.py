import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
SOCLIP_URL = "https://api.soclip.dev/v1/media"
SOCLIP_KEY = os.getenv("SOCLIP_API_KEY", "").strip()

app = FastAPI(title="YouTube Downloader")


class UrlRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"
    media_type: str = "video"


def validate_youtube(url: str) -> str:
    url = url.strip()
    host = (urlparse(url).hostname or "").lower()

    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return url

    raise ValueError("Нужна корректная ссылка YouTube.")


async def soclip_media(url: str):
    if not SOCLIP_KEY:
        raise HTTPException(
            500,
            "SOCLIP_API_KEY не задан в Render → Environment."
        )

    headers = {
        "Authorization": f"Bearer {SOCLIP_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {"url": url}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                SOCLIP_URL,
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Soclip недоступен: {exc}")

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            502,
            f"Soclip вернул не JSON: {response.text[:1000]}"
        )

    if response.status_code >= 400 or not data.get("success"):
        message = (
            data.get("error")
            or data.get("message")
            or f"Soclip HTTP {response.status_code}"
        )
        raise HTTPException(response.status_code or 502, str(message))

    return data


def normalize_formats(medias):
    result = []
    seen = set()

    for media in medias or []:
        ext = (media.get("ext") or "").lower()
        width = media.get("width")
        height = media.get("height")
        url = media.get("url")

        if not url:
            continue

        if ext != "mp4":
            continue

        try:
            height = int(height or 0)
        except Exception:
            height = 0

        if height <= 0:
            continue

        key = (height, ext)

        if key in seen:
            continue

        seen.add(key)

        result.append({
            "quality": f"{height}p",
            "height": height,
            "width": width,
            "ext": ext,
            "url": url,
            "label": media.get("label") or f"mp4 ({height}p)",
        })

    result.sort(key=lambda x: x["height"], reverse=True)
    return result


@app.get("/")
def root():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "Soclip",
        "key_configured": bool(SOCLIP_KEY),
    }


@app.post("/api/info")
async def info(req: UrlRequest):
    try:
        url = validate_youtube(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    data = await soclip_media(url)
    payload = data.get("data") or {}

    formats = normalize_formats(payload.get("medias"))

    return {
        "success": True,
        "title": payload.get("title") or "YouTube video",
        "thumbnail": payload.get("thumbnail") or "",
        "duration": payload.get("duration"),
        "uploader": payload.get("author") or "",
        "formats": formats,
        "audio": [
            m for m in (payload.get("medias") or [])
            if (m.get("ext") or "").lower() in {"mp3", "m4a", "opus"}
            and m.get("url")
        ],
        "credits_used": data.get("credits_used"),
        "credits_remaining": data.get("credits_remaining"),
    }


@app.post("/api/download")
async def download(req: DownloadRequest):
    try:
        url = validate_youtube(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    data = await soclip_media(url)
    payload = data.get("data") or {}
    medias = payload.get("medias") or []

    quality = req.quality.lower().strip()

    if req.media_type == "audio":
        candidates = [
            m for m in medias
            if (m.get("ext") or "").lower() in {"mp3", "m4a", "opus"}
            and m.get("url")
        ]
        if not candidates:
            raise HTTPException(502, "Soclip не вернул аудиофайл.")

        chosen = candidates[0]
        return {
            "success": True,
            "download_url": chosen["url"],
            "filename": f"{payload.get('title') or 'youtube-audio'}.{chosen.get('ext') or 'mp3'}",
            "quality": chosen.get("label") or chosen.get("ext"),
            "credits_used": data.get("credits_used"),
            "credits_remaining": data.get("credits_remaining"),
        }

    target = None

    if quality == "best":
        mp4 = normalize_formats(medias)
        if mp4:
            target = mp4[0]
    else:
        try:
            wanted = int(quality.rstrip("p"))
        except ValueError:
            wanted = 1080

        mp4 = normalize_formats(medias)
        mp4 = [m for m in mp4 if m["height"] <= wanted]

        if mp4:
            target = mp4[0]

    if not target:
        raise HTTPException(
            502,
            "Soclip не вернул подходящее MP4-качество."
        )

    title = payload.get("title") or "youtube-video"

    return {
        "success": True,
        "download_url": target["url"],
        "filename": f"{title}.{target['ext']}",
        "quality": target["quality"],
        "credits_used": data.get("credits_used"),
        "credits_remaining": data.get("credits_remaining"),
    }
