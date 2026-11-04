# The Sandcage API image. The Docker socket proxy runs from the same image with another command.
# Build context: the repository root. Every dependency is pinned with hashes (requirements.txt is
# generated from uv.lock; a test keeps the two in step) and installed without resolving anything.
FROM python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-deps --require-hashes -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt \
    && groupadd --gid 10001 sandcage \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandcage

COPY src ./src
COPY demo-repos ./demo-repos
COPY scripts/api_with_key_file.py ./scripts/api_with_key_file.py

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=4s --start-period=20s --retries=5 \
    CMD ["python", "-I", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

CMD ["python", "-P", "scripts/api_with_key_file.py"]
