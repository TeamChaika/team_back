FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/.local \
    && chown appuser:appuser /app/.local

COPY main.py ./
COPY app/ ./app/
COPY supabase/migrations/ ./supabase/migrations/

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; p=os.environ.get('PORT','').strip() or '8000'; urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "app.serve"]
