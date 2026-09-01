FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno for yt-dlp YouTube JavaScript challenges
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL=/root/.deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

RUN pip install --no-cache-dir -U "yt-dlp[default]" fastapi "uvicorn[standard]" pydantic

WORKDIR /app

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]