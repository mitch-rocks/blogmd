# BlogMD — markdown blog with a writers area
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# app code + the markdown content that is imported into the database on first start
COPY src ./src
COPY content ./content

# unprivileged user; /data is only used if you choose SQLite or file:// media instead of MongoDB
RUN useradd --system --uid 10001 --no-create-home --home-dir /app blogmd \
 && mkdir -p /data \
 && chown blogmd:blogmd /data

# defaults match docker-compose.yml (MongoDB service named "mongo", uploads in GridFS)
ENV DATABASE_URL=mongodb://mongo:27017/blogmd \
    MEDIA_URL=gridfs:// \
    HOST=0.0.0.0 \
    PORT=8001

USER blogmd
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8001\")}/healthz', timeout=4).status == 200 else 1)"

# serve (uvicorn/FastAPI): waits for the database, imports content on first run,
# creates/repairs the admin account from ADMIN_* env vars
CMD ["python", "src/main.py", "serve"]
