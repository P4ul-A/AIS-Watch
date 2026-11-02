FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AIS_DATA_DIR=/data PORT=8080
WORKDIR /app
RUN groupadd --gid 1000 ais && useradd --uid 1000 --gid ais --create-home ais \
    && mkdir /data && chown ais:ais /data
COPY pyproject.toml requirements.lock README.md ./
COPY ais_watch/ ./ais_watch/
COPY scripts/ ./scripts/
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps .
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1
ENTRYPOINT ["python", "-m", "ais_watch.bootstrap"]
