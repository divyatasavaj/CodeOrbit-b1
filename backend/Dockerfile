# CodeOracle backend - long-running FastAPI service.
# Use this image on container hosts (Render Docker, Fly.io, Railway, a VPS).
# Vercel serverless CANNOT run this service: it needs a writable disk for the
# job store and analyses run for minutes, well past the serverless timeout.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEORACLE_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Job store + LLM/op cache live here. Mount a volume at /data to survive
# restarts and redeploys (otherwise results are lost when the container is
# replaced).
RUN mkdir -p /data

EXPOSE 8000

# Hosts pass $PORT; fall back to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]