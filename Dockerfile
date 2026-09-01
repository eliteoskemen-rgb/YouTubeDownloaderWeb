FROM node:20-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

RUN git clone --depth 1 --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil

WORKDIR /opt/bgutil/server

RUN npm ci --omit=dev \
    && npx tsc

WORKDIR /app

COPY . .

ENV YT_DLP_POT_PROVIDER_URL=http://127.0.0.1:4416

RUN chmod +x start.sh

CMD ["./start.sh"]