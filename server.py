import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
TUNELIO_BASE = "https://tunelio.dev"
TUNELIO_KEY = os.getenv("TUNELIO_KEY", "").strip()
YANDEX_PUBLIC_EXE = "https://disk.yandex.kz/d/CnupjPQlRoDulg"
APP_EXE = BASE_DIR / "YouTubeDownloader-Setup.exe"

app = FastAPI(title="YouTube Downloader")


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str


def youtube_url(url: str) -> str:
    url = url.strip()
    p = urlparse(url)
    host = (p.hostname or "").lower()

    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
        if vid:
            return f"https://youtu.be/{vid}"

    if host == "youtube.com" or host.endswith(".youtube.com"):
        q = parse_qs(p.query)
        if q.get("v"):
            return f"https://www.youtube.com/watch?v={q['v'][0]}"

        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return f"https://www.youtube.com/{parts[0]}/{parts[1]}"

    raise ValueError("Нужна корректная ссылка YouTube.")


def api_get(path: str, params: dict):
    if not TUNELIO_KEY:
        raise HTTPException(
            500,
            "Не задан TUNELIO_KEY. Добавь его в Render → Environment."
        )

    try:
        r = requests.get(
            f"{TUNELIO_BASE}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {TUNELIO_KEY}",
                "Accept": "application/json",
            },
            timeout=90,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Tunelio недоступен: {e}")

    try:
        data = r.json()
    except ValueError:
        raise HTTPException(502, "Tunelio вернул некорректный ответ.")

    if r.status_code >= 400:
        message = data.get("message") or data.get("error") or "Ошибка Tunelio API."
        raise HTTPException(r.status_code, message)

    return data


@app.get("/")
def root():
    return FileResponse(BASE_DIR / "index.html")



@app.get("/download-app")
def download_app():
    # Use the stable public Yandex Disk share page instead of redirecting
    # through downloader.disk.yandex.ru, which can return ERR_INVALID_RESPONSE.
    return RedirectResponse(
        url=YANDEX_PUBLIC_EXE,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )

@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "tunelio",
        "key_configured": bool(TUNELIO_KEY),
        "app_available": True,
        "app_source": "Yandex Disk share page",
    }


@app.post("/api/info")
def info(req: InfoRequest):
    try:
        url = youtube_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    data = api_get("/info", {"url": url})

    return {
        "title": data.get("title", "YouTube video"),
        "thumbnail": data.get("thumbnail", ""),
        "duration_seconds": data.get("duration_seconds"),
        "duration_str": data.get("duration_str", ""),
        "formats": data.get("formats", []),
        "audioFormat": data.get("audioFormat"),
    }


@app.post("/api/download")
def download(req: DownloadRequest):
    try:
        url = youtube_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    quality = req.quality.strip().lower()
    allowed = {"144p", "240p", "360p", "480p", "720p", "mp3"}
    if quality not in allowed:
        raise HTTPException(403, "На сайте бесплатно доступны только 144p–720p и MP3. Для 1080p/4K скачайте приложение YouTube Downloader для Windows.")

    data = api_get(
        "/create",
        {"url": url, "quality": quality},
    )

    if data.get("status") != "ok" or not data.get("url"):
        raise HTTPException(
            502,
            data.get("message") or "Tunelio не создал ссылку.",
        )

    return {
        "success": True,
        "download_url": data["url"],
        "filename": data.get("filename", ""),
        "quality": data.get("quality", quality),
        "file_size_str": data.get("file_size_str", ""),
        "expires": data.get("expires"),
    }
