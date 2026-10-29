
FROM python:3.11.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    HOME=/home/ui

WORKDIR /app

RUN groupadd --gid 10003 ui \
    && useradd --uid 10003 --gid 10003 \
        --create-home --home-dir /home/ui \
        --shell /usr/sbin/nologin ui \
    && mkdir -p /home/ui/.streamlit \
    && chown -R 10003:10003 /home/ui

COPY streamlit-ui/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=10003:10003 streamlit-ui/ /app/

USER 10003:10003

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s \
    --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4); \
raise SystemExit(0 if r.status == 200 else 1)"

CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", "--server.port=8501"]
