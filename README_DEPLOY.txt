YouTube Downloader — Tunelio web 720p + Windows app for 4K

Files:
- server.py
- index.html
- requirements.txt
- Dockerfile

The Windows installer is NOT stored in GitHub.
The website button /download-app resolves the public Yandex Disk link:
https://disk.yandex.kz/d/CnupjPQlRoDulg

Render:
- Runtime: Docker
- Dockerfile Path: ./Dockerfile
- Docker Build Context: .
- Docker Command: empty
- Health Check Path: /health
- Environment:
    TUNELIO_KEY = your Tunelio key

Website:
- 144p–720p + MP3 through Tunelio
- 4K/high quality → Windows application download
- UI languages: Russian, Kazakh, English
