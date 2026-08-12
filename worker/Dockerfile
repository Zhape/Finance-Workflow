# Portable image: runs unchanged on Render, Fly, Cloud Run or a plain VM.
# Nothing in here is platform-specific, so switching host is a config change.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so code edits do not invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fw ./fw

# Run as a non-root user. The worker holds other companies' refresh tokens;
# there is no reason for it to be able to write to its own image.
RUN useradd --create-home --uid 10001 worker
USER worker

EXPOSE 8000

# Hosts inject $PORT (Render, Cloud Run); default to 8000 for Fly and local.
# One worker process on purpose: runs are dispatched to an in-process thread
# pool, so a second process would just add cold-start cost for no throughput.
CMD ["sh", "-c", "exec uvicorn fw.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 75"]
