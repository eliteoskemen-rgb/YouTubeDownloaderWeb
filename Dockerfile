# =========================================================
# Build bgutil PO Token provider
# =========================================================

FROM node:26-bookworm-slim AS pot-builder

WORKDIR /opt

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone \
    --depth 1 \
    --branch 1.3.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil

WORKDIR /opt/bgutil/server

RUN npm ci --no-audit --no-fund

RUN npx tsc


# =========================================================
# Final application
# =========================================================

FROM node:26-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-pip \
       ffmpeg \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------
# Copy PO Token provider
# ---------------------------------------------------------

RUN mkdir -p /opt/bgutil/server

COPY --from=pot-builder \
    /opt/bgutil/server/build \
    /opt/bgutil/server/build

COPY --from=pot-builder \
    /opt/bgutil/server/node_modules \
    /opt/bgutil/server/node_modules

COPY --from=pot-builder \
    /opt/bgutil/server/package.json \
    /opt/bgutil/server/package.json

# ---------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------

COPY requirements.txt .

RUN python3 -m pip install \
    --break-system-packages \
    --no-cache-dir \
    -r requirements.txt

# ---------------------------------------------------------
# Application
# ---------------------------------------------------------

COPY server.py .
COPY index.html .

COPY start.sh .

RUN chmod +x start.sh

RUN mkdir -p /app/downloads

EXPOSE 10000

CMD ["./start.sh"]