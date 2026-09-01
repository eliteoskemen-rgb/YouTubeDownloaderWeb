FROM node:26-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# -----------------------------------------
# System packages
# -----------------------------------------

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------
# Python
# -----------------------------------------

COPY requirements.txt .

RUN python3 -m pip install \
    --break-system-packages \
    --no-cache-dir \
    -r requirements.txt

# -----------------------------------------
# BGUTIL PO TOKEN PROVIDER
# -----------------------------------------

WORKDIR /opt

RUN git clone \
    --depth 1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    bgutil

WORKDIR /opt/bgutil/server

RUN npm ci --omit=dev --no-audit --no-fund

RUN npm run build

# -----------------------------------------
# yt-dlp PO Token plugin
# -----------------------------------------

RUN python3 -m pip install \
    --break-system-packages \
    --no-cache-dir \
    -U bgutil-ytdlp-pot-provider

# -----------------------------------------
# Back to application
# -----------------------------------------

WORKDIR /app

COPY . .

RUN chmod +x start.sh

ENV POT_PROVIDER_URL=http://127.0.0.1:4416

ENV PATH="/usr/local/bin:${PATH}"

EXPOSE 8000

CMD ["./start.sh"]