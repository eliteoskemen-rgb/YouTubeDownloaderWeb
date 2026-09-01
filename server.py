import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
SOCLIP_URL = "https://api.soclip.dev/v1/media"
SOCLIP_KEY = os.getenv("SOCLIP_API_KEY", "").strip()

app = FastAPI(title="YouTube Downloader")
download_links = {}


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
        raise RuntimeError(
            "SOCLIP_API_KEY не задан в Render → Environment."
        )

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

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Soclip вернул не JSON (HTTP {response.status_code}): "
            f"{response.text[:2000]}"
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
            str(
                data.get("error")
                or data.get("message")
                or "Soclip extraction failed"
            )
        )

    return data


def payload_of(data: dict) -> dict:
    payload = data.get("data")
    return payload if isinstance(payload, dict) else data


def media_list(data: dict):
    medias = payload_of(data).get("medias", [])
    return medias if isinstance(medias, list) else []


def safe_filename(title: str, ext: str) -> str:
    name = re.sub(
        r'[\\/:*?"<>|\r\n]+',
        "_",
        title or "youtube-video",
    ).strip(" .")

    if not name:
        name = "youtube-video"

    return f"{name[:140]}.{ext}"


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

        size = media.get("filesize") or media.get("file_size")
        size_str = media.get("file_size_str") or ""

        if not size_str and size:
            try:
                size_str = f"{size / 1024 / 1024:.1f} MB"
            except Exception:
                pass

        result.append(
            {
                "quality": f"{height}p",
                "height": height,
                "width": media.get("width"),
                "ext": ext,
                "url": url,
                "label": media.get("label") or f"{ext} ({height}p)",
                "file_size_str": size_str,
            }
        )

    result.sort(key=lambda x: x["height"], reverse=True)
    return result


def audio_media(data: dict):
    result = []

    for media in media_list(data):
        if not isinstance(media, dict):
            continue

        url = media.get("url")
        ext = str(media.get("ext") or "").lower()

        if not url:
            continue

        if ext in {"mp3", "m4a", "opus"}:
            result.append(media)

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
        data = await soclip_request(url)
        payload = payload_of(data)

        return {
            "success": True,
            "title": payload.get("title") or "YouTube video",
            "thumbnail": payload.get("thumbnail") or "",
            "duration": payload.get("duration"),
            "uploader": payload.get("author") or "",
            "formats": normalize_formats(data),
            "audio": audio_media(data),
            "credits_used": data.get("credits_used"),
            "credits_remaining": data.get("credits_remaining"),
        }

    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
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
            filename = safe_filename(
                payload.get("title") or "youtube-audio",
                ext,
            )

        else:
            quality = req.quality.strip().lower()

            videos = []
            for media in medias:
                if not isinstance(media, dict):
                    continue
                if not media.get("url"):
                    continue
                ext = str(media.get("ext") or "").lower()
                if ext not in {"mp4", "webm"}:
                    continue

                try:
                    height = int(media.get("height") or 0)
                except (TypeError, ValueError):
                    height = 0

                if height > 0:
                    videos.append((height, media))

            if not videos:
                raise RuntimeError(
                    "Soclip не вернул подходящее видео."
                )

            if quality != "best":
                try:
                    target = int(quality.rstrip("p"))
                except ValueError:
                    target = max(h for h, _ in videos)

                filtered = [
                    (h, m)
                    for h, m in videos
                    if h <= target
                ]
                if filtered:
                    videos = filtered

            videos.sort(
                key=lambda item: (
                    item[0],
                    1 if str(item[1].get("ext") or "").lower() == "mp4" else 0,
                ),
                reverse=True,
            )

            _, chosen = videos[0]
            ext = str(chosen.get("ext") or "mp4").lower()
            filename = safe_filename(
                payload.get("title") or "youtube-video",
                ext,
            )

        token = uuid.uuid4().hex

        download_links[token] = {
            "url": chosen["url"],
            "filename": filename,
            "content_type": (
                "audio/mpeg"
                if req.media_type == "audio"
                else (
                    "video/mp4"
                    if ext == "mp4"
                    else "video/webm"
                )
            ),
        }

        return {
            "success": True,
            "download_url": f"/api/file/{token}",
            "filename": filename,
            "quality": chosen.get("label") or ext,
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
        raise HTTPException(
            404,
            "Ссылка на файл истекла или не найдена.",
        )

    target_url = item["url"]
    filename = item["filename"]
    content_type = item["content_type"]

    # Download the upstream file completely to disk first.
    # This prevents the browser from receiving a 0-byte "download"
    # when googlevideo/Soclip rejects the upstream request midway.
    temp_dir = BASE_DIR / "downloads"
    temp_dir.mkdir(parents=True, exist_ok=True)

    token_dir = temp_dir / token
    token_dir.mkdir(parents=True, exist_ok=True)

    file_path = token_dir / filename

    try:
        timeout = httpx.Timeout(
            1800.0,
            connect=30.0,
            read=180.0,
            write=180.0,
            pool=30.0,
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            http2=True,
        ) as client:
            async with client.stream(
                "GET",
                target_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/150.0 Safari/537.36"
                    ),
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Referer": "https://www.youtube.com/",
                },
            ) as response:

                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        f"Источник Soclip вернул HTTP "
                        f"{response.status_code}: "
                        f"{body[:1000].decode('utf-8', 'replace')}"
                    )

                content_length = response.headers.get(
                    "content-length"
                )

                with file_path.open("wb") as out_file:
                    async for chunk in response.aiter_bytes(
                        4 * 1024 * 1024
                    ):
                        out_file.write(chunk)

                if not file_path.exists() or file_path.stat().st_size == 0:
                    raise RuntimeError(
                        "Источник вернул пустой файл."
                    )

                if content_length:
                    try:
                        expected = int(content_length)
                        actual = file_path.stat().st_size
                        if expected > 0 and actual != expected:
                            raise RuntimeError(
                                f"Неполная загрузка: получено "
                                f"{actual} из {expected} байт."
                            )
                    except ValueError:
                        pass

        # The token is one-time only, but remove it after a successful
        # upstream transfer, not before it.
        download_links.pop(token, None)

        ascii_name = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            filename,
        ).strip("_") or "download"

        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )

        response = FileResponse(
            path=file_path,
            media_type=content_type,
            filename=filename,
            headers={
                "Content-Disposition": disposition,
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

        # Best-effort cleanup after response completion isn't trivial with
        # FileResponse; leave the file in the ephemeral Render filesystem.
        return response

    except HTTPException:
        raise

    except Exception as exc:
        try:
            if file_path.exists():
                file_path.unlink()
            if token_dir.exists() and not any(token_dir.iterdir()):
                token_dir.rmdir()
        except Exception:
            pass

        raise HTTPException(
            502,
            f"Не удалось скачать файл с Soclip: {exc}",
        )
