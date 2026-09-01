YouTube Downloader — Tunelio / 720p free / 4K Windows app

1) Put YouTubeDownloader-Setup.exe into the repository root, next to server.py and Dockerfile.
2) In Render Environment add:
   TUNELIO_KEY = your Tunelio API key
3) Docker:
   Dockerfile Path: ./Dockerfile
   Docker Build Context: .
   Docker Command: empty
   Health Check Path: /health
4) Push:
   git add .
   git commit -m "Tunelio 720p free plus Windows 4K app"
   git push origin main

Website:
- free 144p–720p + MP3
- all qualities above 720p are intentionally blocked on the web endpoint
- /download-app downloads YouTubeDownloader-Setup.exe
