FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    MEDITOK_DB_PATH=/data/meditok.db \
    MEDITOK_UPLOAD_DIR=/data/uploads \
    SESSION_COOKIE_SECURE=1

WORKDIR /app
RUN addgroup --system meditalk && adduser --system --ingroup meditalk meditalk

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN mkdir -p /data/uploads && \
    chown -R meditalk:meditalk /app /data && \
    chmod +x /app/docker-entrypoint.sh

USER meditalk
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
