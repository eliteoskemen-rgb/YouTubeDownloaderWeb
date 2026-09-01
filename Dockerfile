FROM node:26-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PORT=10000

WORKDIR /app

# =========================================================
# System packages
# =========================================================

RUN apt-get update && apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# =========================================================
# Python virtual environment
# =========================================================

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# =========================================================
# BgUtils PO Token Provider
# =========================================================

RUN git clone \
    --depth 1 \
    --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil

WORKDIR /opt/bgutil/server

RUN npm ci --omit=dev \
    && npm ci --no-audit --no-fund \
    && npx tsc

# =========================================================
# Application
# =========================================================

WORKDIR /app

COPY . .

RUN mkdir -p /app/downloads

# =========================================================
# Start
# =========================================================

RUN chmod +x /app/start.sh

EXPOSE 10000

CMD ["/app/start.sh"]