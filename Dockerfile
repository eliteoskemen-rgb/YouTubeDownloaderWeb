FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20+
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U bgutil-ytdlp-pot-provider

# Install PO Token provider server
RUN git clone --single-branch --branch 1.3.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

COPY . .

RUN mkdir -p /app/downloads

EXPOSE 10000

CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js --port 4416 & exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]