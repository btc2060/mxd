# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
WORKDIR /app
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends chromium fonts-wqy-zenhei; \
    else apt-get update && apt-get install -y --no-install-recommends chromium fonts-wqy-zenhei; fi && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca python -m pip install --no-cache-dir -r requirements.txt; \
    else python -m pip install --no-cache-dir -r requirements.txt; fi
COPY goldwatch ./goldwatch
COPY static ./static
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"
CMD ["python", "-m", "uvicorn", "goldwatch.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
