
FROM python:3.11.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_NO_ADVISORY_WARNINGS=1 \
    EMBEDDING_CACHE_DIR=/data/cache \
    HF_HOME=/data/cache \
    HF_HUB_CACHE=/data/cache

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-spa \
        tesseract-ocr-deu \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10002 agent \
    && useradd --uid 10002 --gid 10002 \
        --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /data/incoming /data/reports /data/cache \
        /config \
    && chown -R 10002:10002 /data /config \
    && chmod 777 /data/cache

COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=10002:10002 agent/app/ /app/app/


USER 10002:10002

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s \
    --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8); \
raise SystemExit(0 if r.status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
