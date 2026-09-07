# The FastAPI service. Deliberately does NOT contain torch or FlagEmbedding:
# the app calls the embedding and rerank models over HTTP and never imports
# them, so bundling them would add gigabytes to every deploy of the API.
# Those models are their own service (Dockerfile.models).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

WORKDIR /srv

# Compilers are needed by a few sdists and by nothing at run time, so they are
# installed and removed inside one layer rather than shipped.
COPY requirements.txt ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

# Chunking tokenizes with BGE-M3's tokenizer. Fetching it at first upload
# would make that request slow and would need outbound network from a
# container that otherwise needs none, so it is baked in (tokenizer only —
# a few MB, not the 2.3GB model).
RUN python -c "from transformers import AutoTokenizer; \
AutoTokenizer.from_pretrained('BAAI/bge-m3')"

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Uploads live on a volume; the app must be able to write it as a non-root user.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /srv/storage \
 && chown -R app:app /srv
USER app

EXPOSE 8000

# curl is not in the slim image, and adding it just for a healthcheck would
# widen the attack surface of a container that needs no HTTP client.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers alone is not enough: uvicorn only believes X-Forwarded-For
# from an address in FORWARDED_ALLOW_IPS, which defaults to 127.0.0.1. Behind
# a proxy container that default silently drops the real client address, and
# every request then looks like it came from the proxy — collapsing the
# per-IP rate limit into one bucket shared by all users.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
