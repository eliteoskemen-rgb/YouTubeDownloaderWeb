FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    curl \
    ca-certificates \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is the recommended JS runtime for current yt-dlp/EJS.
RUN curl -fsSL https://deno.land/install.sh | sh

# Install the bgutil provider source and compile its one-shot generator.
RUN git clone --depth 1 --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY server.py .
COPY index.html .

RUN mkdir -p /app/downloads

ENV BGUTIL_SCRIPT=/opt/bgutil/server/build/generate_once.js

EXPOSE 10000

CMD ["sh", "-c", "exec python3 -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
