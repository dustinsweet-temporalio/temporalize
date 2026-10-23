# One image for the web app and the indexer CLI.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git: Temporalize clones repositories, branches the POC workspace and pushes it
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Dependencies first (cached unless they change)
COPY code_atlas/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Indexer (installs the `semantic-ingress` command)
COPY semantic_ingress /tmp/semantic_ingress
RUN pip install /tmp/semantic_ingress && rm -rf /tmp/semantic_ingress

# Web app and UI
COPY code_atlas /app/code_atlas
COPY web /app/web

# Temporal knowledge pack (lens grounding)
COPY knowledge /app/knowledge

# Non-root user; /data holds the mirrored source and Temporalize's clones and workspaces (named volumes in compose)
RUN useradd -m -u 1000 atlas && mkdir -p /data/source /data/tz/clones /data/tz/workspaces && chown -R atlas:atlas /data /app
USER atlas

WORKDIR /app/code_atlas
ENV SOURCE_DIR=/data/source \
    TZ_DATA_DIR=/data/tz \
    WEB_DIR=/app/web

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
