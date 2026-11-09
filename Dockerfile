# HBM AI Visibility Audits
#   docker build -t hbm-aeo .
#   docker run -p 8080:8080 -v hbm-data:/data --env-file .env hbm-aeo
FROM python:3.12-slim

# Chromium prints the PDF reports; fonts so the PDFs render properly
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu-core fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 PORT=8080 HBM_DATA_DIR=/data HBM_USE_REA=0 HBM_CHROME=/usr/bin/chromium APP_ENV=production

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aeo ./aeo
COPY webapp ./webapp
COPY tools ./tools
COPY data/hbm-logo.webp ./data/hbm-logo.webp
COPY audit.py manage.py serve.py spend.py setup_keys.py batch.py ./

RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"
CMD ["python", "serve.py"]
