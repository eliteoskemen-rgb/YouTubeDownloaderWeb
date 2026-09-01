FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV YTCUI_BIN=/opt/ytcui-dl/ytcui-dl

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    g++ \
    make \
    git \
    ca-certificates \
    libssl-dev \
    zlib1g-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git clone --depth 1 https://github.com/MilkmanAbi/ytcui-dl.git /opt/ytcui-dl \
    && make -C /opt/ytcui-dl

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY server.py .
COPY index.html .

RUN mkdir -p /app/downloads

EXPOSE 10000

CMD ["sh", "-c", "exec python3 -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
