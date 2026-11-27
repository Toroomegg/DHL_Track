FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN pip install --no-cache-dir "playwright==1.58.0" "gunicorn>=23,<25" \
    && python -m playwright install --with-deps --only-shell chromium \
    && useradd --create-home app

WORKDIR /app
COPY dhl_tracking.py render_app.py ./
USER app
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 2 --timeout 100 render_app:application"]

