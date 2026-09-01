import os
from pathlib import Path
from urllib.parse import urlparse, quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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


async def soclip_request(url: str):
    if not SOCLIP_KEY:
        raise RuntimeError("SOCLIP_API_KEY не задан в Render Environment.")

    headers = {
        "Authorization": f"Bearer {SOCLIP_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=120,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                SOCLIP_URL,
                headers=headers,
                json={"url": url},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Soclip недоступен: {exc}")

    raw = response.text

    try:
        data = response.json()
    except ValueError:
        # Keep the original upstream response visible as a normal JSON error.
        raise RuntimeError(
            f"Soclip вернул не JSON (HTTP {response.status_code}): {raw[:2000]}"
        )

    if response.status_code >= 400:
        message = (
            data.get("error")
            or data.get("message")
            or data.get("detail")
            or f"Soclip HTTP {response.status_code}"
        )
        raise RuntimeError(str(message))

    if data.get("success") is False:
        raise RuntimeError(
            str(data.get("error") or data.get("message") or "Soclip extraction failed")
        )

    return data


def payload_of(data: dict) -> dict:
    payload = data.get("data")
    if isinstance(payload, dict):
        return payload
    return data


def media_list(data: dict):
    payload = payload_of(data)
    medias = payload.get("medias", [])
    return medias if isinstance(medias, list) else []


def normalize_formats(data: dict):
    result = []
    seen = set()

    for media in media_list(data):
        if not isinstance(media, dict):
            continue

        ext = str(media.get("ext") or "").lower()
        url = media.get("url")

        if ext not in {"mp4", "webm"} or not url:
            continue

        try:
            height = int(media.get("height") or 0)
        except (TypeError, ValueError):
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
            "width": media.get("width"),
            "ext": ext,
            "url": url,
            "label": media.get("label") or f"{ext} ({height}p)",
            "file_size": media.get("filesize") or media.get("file_size"),
            "file_size_str": media.get("file_size_str") or "",
        })

    result.sort(key=lambda x: x["height"], reverse=True)
    return result


def audio_media(data: dict):
    out = []
    for media in media_list(data):
        if not isinstance(media, dict) or not media.get("url"):
            continue
        ext = str(media.get("ext") or "").lower()
        if ext in {"mp3", "m4a", "opus", "webm"} and (
            "audio" in str(media.get("label") or "").lower()
            or ext in {"mp3", "m4a", "opus"}
        ):
            out.append(media)
    return out


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
        data = await soclip_request(url)
        payload = payload_of(data)

        formats = normalize_formats(data)
        audios = audio_media(data)

        return {
            "success": True,
            "title": payload.get("title") or "YouTube video",
            "thumbnail": payload.get("thumbnail") or "",
            "duration": payload.get("duration"),
            "uploader": payload.get("author") or "",
            "formats": formats,
            "audio": audios,
            "credits_used": data.get("credits_used"),
            "credits_remaining": data.get("credits_remaining"),
        }

    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        # Always return proper JSON so the frontend never gets
        # "Unexpected token 'I'".
        raise HTTPException(502, str(exc))


@app.post("/api/download")
async def download(req: DownloadRequest):
    try:
        url = validate_youtube(req.url)
        data = await soclip_request(url)
        payload = payload_of(data)
        medias = media_list(data)

        if req.media_type == "audio":
            candidates = audio_media(data)
            if not candidates:
                raise RuntimeError("Soclip не вернул аудиофайл.")

            chosen = candidates[0]
            ext = str(chosen.get("ext") or "mp3").lower()

            safe_title = re.sub(r'[\/:*?"<>|\r\n]+', "_", payload.get("title") or "youtube-audio").strip()[:140] or "youtube-audio"
            filename = f"{safe_title}.{ext}"
            token = uuid.uuid4().hex
            download_links[token] = {
                "url": chosen["url"],
                "filename": filename,
                "content_type": "audio/mpeg" if ext == "mp3" else "application/octet-stream",
            }
            return {
                "success": True,
                "download_url": f"/api/file/{token}",
                "filename": filename,
                "quality": chosen.get("label") or ext,
                "credits_used": data.get("credits_used"),
                "credits_remaining": data.get("credits_remaining"),
            }

        # Video
        target_height = 0
        quality = req.quality.strip().lower()

        if quality != "best":
            try:
                target_height = int(quality.rstrip("p"))
            except ValueError:
                target_height = 2160

        videos = [
            m for m in medias
            if isinstance(m, dict)
            and m.get("url")
            and str(m.get("ext") or "").lower() in {"mp4", "webm"}
        ]

        def h(m):
            try:
                return int(m.get("height") or 0)
            except (TypeError, ValueError):
                return 0

        videos.sort(
            key=lambda m: (
                h(m),
                1 if str(m.get("ext") or "").lower() == "mp4" else 0,
            ),
            reverse=True,
        )

        if target_height:
            filtered = [m for m in videos if h(m) <= target_height]
            if filtered:
                videos = filtered

        if not videos:
            raise RuntimeError("Soclip не вернул подходящее видео.")

        chosen = videos[0]
        ext = str(chosen.get("ext") or "mp4").lower()

        safe_title = re.sub(r'[\/:*?"<>|\r\n]+', "_", payload.get("title") or "youtube-video").strip()[:140] or "youtube-video"
        filename = f"{safe_title}.{ext}"
        token = uuid.uuid4().hex
        download_links[token] = {
            "url": chosen["url"],
            "filename": filename,
            "content_type": "video/mp4" if ext == "mp4" else "video/webm",
        }

        return {
            "success": True,
            "download_url": f"/api/file/{token}",
            "filename": filename,
            "quality": chosen.get("label") or f"{h(chosen)}p",
            "credits_used": data.get("credits_used"),
            "credits_remaining": data.get("credits_remaining"),
        }

    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc))



@app.get("/api/file/{token}")
async def download_file(token: str):
    item = download_links.get(token)
    if not item:
        raise HTTPException(404, "Ссылка на файл истекла или не найдена.")

    target_url = item["url"]
    filename = item["filename"]
    content_type = item["content_type"]

    # Keep the original Soclip URL server-side. The browser only sees
    # this same-origin endpoint, so it cannot navigate to googlevideo.com.
    async def stream():
        timeout = httpx.Timeout(900.0, connect=30.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            async with client.stream(
                "GET",
                target_url,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        f"Источник Soclip вернул HTTP {response.status_code}: "
                        f"{body[:800].decode('utf-8', 'replace')}"
                    )

                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk

    safe_ascii = re.sub(r'[^A-Za-z0-9._-]+', '_', filename).strip('_') or "download"
    disposition = (
        f'attachment; filename="{safe_ascii}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )

    return StreamingResponse(
        stream(),
        media_type=content_type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
