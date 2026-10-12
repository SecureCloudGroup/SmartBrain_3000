# SmartBrain_3000 — renderer sidecar (Round 20, ni-format §35): the ONLY place a page-browser
# engine runs in a Docker install. The app container never runs one.
#
# Built FROM the app's base image with the app package, so the sidecar's agent reuses the app's
# own process code (spawn/watch/kill) and the engine is installed at build time by the SAME
# installer (pinned GitHub URL, size cap, sha256 before unpacking, member hashes) — the build
# fails unless the installed engine re-hashes clean. At run time (compose profile `render`) it
# has no network (network_mode: none), a read-only root, no capabilities, no app data volume;
# its only exit is a loopback relay into the app's egress socket on the shared `smartbrain_render`
# volume. Build context: the repo root (`docker build -f compose/render/Dockerfile .`).
FROM python:3.14-slim

WORKDIR /app
COPY app/ /app/
# The engine lands in /app/data/browsers/<name>/<version>/ — the same path the app renders into
# the job's argv (the agent still substitutes its own verified copy). Readable, never writable.
RUN pip install --no-cache-dir -c requirements.lock . \
    && SMARTBRAIN_CONTAINER=1 SMARTBRAIN_DB_PATH=/app/data/smartbrain.duckdb \
       python -m smartbrain_3000.browsers.render_agent --install \
    && chmod -R a+rX,go-w /app/data/browsers \
    && rm -rf /root/.cache

ARG SMARTBRAIN_VERSION=0.0.0-dev
ENV SMARTBRAIN_CONTAINER=1 \
    SMARTBRAIN_DB_PATH=/app/data/smartbrain.duckdb \
    SMARTBRAIN_RENDER_AGENT=1 \
    SMARTBRAIN_VERSION=${SMARTBRAIN_VERSION}

# uid 65532 with the app's gid (10001): the shared volume is the only thing it can write.
USER 65532:10001

# Exec form: no shell. The agent listens on nothing; jobs arrive as files on /render.
CMD ["python", "-m", "smartbrain_3000.browsers.render_agent"]
