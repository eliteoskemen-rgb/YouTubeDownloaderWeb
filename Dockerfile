FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:$PATH"

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Deno for yt-dlp JavaScript challenges
RUN curl -fsSL https://deno.land/install.sh | sh

COPY requirements.txt .

RUN pip install --no-cache-dir -U \
    -r requirements.txt

COPY server.py .
COPY index.html .

RUN mkdir -p /app/downloads

# Install bgutil provider server
RUN git clone --depth 1 \
    --branch 1.3.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil

WORKDIR /opt/bgutil/server

RUN npm ci --omit=dev --no-audit --no-fund && \
    npm ci --no-audit --no-fund && \
    npx tsc

WORKDIR /app

COPY <<'EOF' /entrypoint.sh
#!/bin/sh
set -e

echo "Starting bgutil PO Token provider..."

node /opt/bgutil/server/build/main.js --port 4416 &

BGUTIL_PID=$!

echo "Waiting for bgutil..."

for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then
        echo "bgutil is ready"
        break
    fi
    sleep 1
done

echo "Starting YouTube Downloader..."

exec python3 -m uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}"
EOF

RUN chmod +x /entrypoint.sh

EXPOSE 10000

CMD ["/entrypoint.sh"]