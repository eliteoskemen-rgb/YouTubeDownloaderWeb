import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
YTDLP_API_URL = os.getenv("YTDLP_API_URL", "http://127.0.0.1:5012").rstrip("/")
YTDLP_API_KEY = os.getenv("YTDLP_API_KEY", "").strip()

app = FastAPI(title="YouTube Downloader")


class UrlRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"
    media_type: str = "video"


def validate_url(url: str):
    url = url.strip()
    host = (urlparse(url).hostname or "").lower()
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return url
    raise ValueError("Нужна ссылка YouTube.")


def headers():
    h = {"Accept": "application/json"}
    if YTDLP_API_KEY:
        h["Authorization"] = f"Bearer {YTDLP_API_KEY}"
    return h


async def api_get(path: str, params: dict):
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        r = await client.get(f"{YTDLP_API_URL}{path}", params=params, headers=headers())
        if r.status_code >= 400:
            text = r.text[:6000]
            raise HTTPException(r.status_code, text or "yt-dlp-api error")
        try:
            return r.json()
        except Exception:
            raise HTTPException(502, "yt-dlp-api вернул не JSON.")


def normalize_info(data):
    # ungaul's API returns the raw yt-dlp info object.
    title = data.get("title") or data.get("fulltitle") or "YouTube video"
    thumbnail = data.get("thumbnail") or ""
    duration = data.get("duration")
    uploader = data.get("uploader") or data.get("channel") or ""

    formats = []
    seen = set()

    for fmt in data.get("formats") or []:
        height = fmt.get("height")
        if not height:
            continue
        try:
            height = int(height)
        except Exception:
            continue
        if height < 144 or height in seen:
            continue
        seen.add(height)
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        size_str = ""
        if size:
            try:
                size_str = f"{size/1024/1024:.1f} MB"
            except Exception:
                pass
        formats.append({
            "quality": f"{height}p",
            "height": height,
            "file_size_str": size_str,
        })

    formats.sort(key=lambda x: x["height"], reverse=True)

    return {
        "success": True,
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "uploader": uploader,
        "formats": formats,
        "audioFormat": "mp3",
    }


@app.get("/")
async def root():
    return FileResponse(BASE / "index.html")


@app.get("/health")
async def health():
    try:
        await api_get("/info", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        api_ok = True
    except Exception:
        api_ok = False
    return {
        "status": "ok",
        "engine": "ungaul/yt-dlp-api",
        "backend": YTDLP_API_URL,
        "backend_ok": api_ok,
    }


@app.post("/api/info")
async def info(req: UrlRequest):
    try:
        url = validate_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    data = await api_get("/info", {"url": url})
    return normalize_info(data)


@app.get("/api/formats")
async def formats(url: str):
    try:
        url = validate_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await api_get("/formats", {"url": url})


@app.post("/api/download")
async def download(req: DownloadRequest):
    try:
        url = validate_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    quality = req.quality.strip().lower()
    if req.media_type == "audio":
        params = {
            "url": url,
            "format": "mp3",
        }
    else:
        params = {
            "url": url,
            "format": "mp4",
        }
        if quality and quality != "best":
            params["quality"] = quality

    # Return the backend endpoint. The browser downloads from yt-dlp-api,
    # keeping the large file off this FastAPI process.
    from urllib.parse import urlencode
    direct = f"{YTDLP_API_URL}/download?{urlencode(params)}"
    return {
        "success": True,
        "download_url": direct,
        "quality": quality,
        "format": params["format"],
    }


@app.get("/api/backend-health")
async def backend_health():
    try:
        data = await api_get("/formats", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        return {"success": True, "backend": YTDLP_API_URL, "response_type": type(data).__name__}
    except Exception as e:
        return {"success": False, "backend": YTDLP_API_URL, "error": str(e)}
