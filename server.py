import asyncio, json, os, re, shutil, subprocess, tempfile, uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE=Path(__file__).resolve().parent
DOWNLOADS=BASE/"downloads"
COOKIES=BASE/"www.youtube.com_cookies.txt"
YT_ARGS=["--cookies",str(COOKIES),"--js-runtimes","deno"]
DOWNLOADS.mkdir(exist_ok=True)
app=FastAPI(title="YouTube Downloader Online")
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")

tasks={}
URL_RE=re.compile(r"^https?://",re.I)

class Link(BaseModel):
    url:str
class Download(Link):
    quality:str="best"
    mode:str="video"

@app.get("/")
def index():
    return FileResponse(BASE/"index.html")

def safe_task():
    tid=uuid.uuid4().hex
    tasks[tid]={"status":"starting","percent":0,"speed":"","eta":"","filename":"","error":"","file":""}
    return tid

@app.post("/api/info")
async def info(x:Link):
    if not URL_RE.match(x.url.strip()): raise HTTPException(400,"Неверная ссылка")
    p=await asyncio.create_subprocess_exec(
        "yt-dlp",*YT_ARGS,"--dump-single-json","--skip-download","--no-playlist",x.url,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE
    )
    out,err=await p.communicate()
    if p.returncode!=0: raise HTTPException(400,err.decode("utf-8","replace")[-1000:])
    try:d=json.loads(out)
    except: raise HTTPException(500,"Не удалось получить информацию")
    return {"title":d.get("title","Видео"),"uploader":d.get("uploader") or d.get("channel",""),"duration":d.get("duration"),"height":d.get("height"),"thumbnail":d.get("thumbnail","")}

@app.post("/api/download")
async def download(x:Download):
    if not URL_RE.match(x.url.strip()): raise HTTPException(400,"Неверная ссылка")
    tid=safe_task()
    asyncio.create_task(run_download(tid,x))
    return {"id":tid}

async def run_download(tid,x):
    t=tasks[tid]
    work=DOWNLOADS/tid;work.mkdir()
    try:
        if x.mode=="audio":
            fmt=["-f","bestaudio/best","-x","--audio-format","mp3","--audio-quality","192K"]
        else:
            if x.quality=="best": fmt=["-f","bestvideo+bestaudio/best","--merge-output-format","mp4"]
            else:
                h=int(x.quality); fmt=["-f",f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best[height<={h}]/best","--merge-output-format","mp4"]
        out=str(work/"%(title)s.%(ext)s")
        cmd=["yt-dlp",*YT_ARGS,"--newline","--progress","--no-playlist","--ffmpeg-location",shutil.which("ffmpeg") or "",*fmt,"-o",out,x.url]
        cmd=[c for c in cmd if c!=""]
        p=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        while True:
            line=await p.stdout.readline()
            if not line: break
            s=line.decode("utf-8","replace").strip()
            m=re.search(r"\[download\]\s+([\d.]+)%.*?at\s+(.+?)\s+ETA\s+(.+)",s)
            if m:
                t["percent"]=float(m.group(1));t["speed"]=m.group(2);t["eta"]=m.group(3)
            if "[download]" in s and "%" in s:
                t["filename"]=s.split("]")[-1].strip()
            if "[Merger]" in s or "Destination:" in s:t["status"]="processing"
        rc=await p.wait()
        if rc!=0: raise RuntimeError("yt-dlp не смог скачать файл")
        candidates=[p for p in work.iterdir() if p.is_file()]
        if not candidates: raise RuntimeError("Файл не найден после загрузки")
        f=max(candidates,key=lambda p:p.stat().st_mtime)
        t.update(status="done",percent=100,filename=f.name,file=str(f))
    except Exception as e:
        t.update(status="error",error=str(e))
    finally:
        pass

@app.get("/api/progress/{tid}")
def progress(tid:str):
    if tid not in tasks: raise HTTPException(404,"Задача не найдена")
    return tasks[tid]

@app.get("/api/file/{tid}")
def file(tid:str):
    t=tasks.get(tid)
    if not t or t["status"]!="done" or not t["file"]: raise HTTPException(404,"Файл ещё не готов")
    f=Path(t["file"])
    if not f.exists(): raise HTTPException(404,"Файл удалён")
    return FileResponse(f,filename=f.name,media_type="application/octet-stream")

# Production deployment example:
# uvicorn server:app --host 0.0.0.0 --port 8000
