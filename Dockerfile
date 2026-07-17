# SmartBrain_3000 — backend image (foundation).
# Builds the FastAPI app and runs it with uvicorn on the internal port 33000.
FROM python:3.14-slim

# Least privilege: run as a dedicated non-root user.
RUN useradd --create-home --uid 10001 smartbrain
WORKDIR /app

# Install only the backend package (build context is the repo root).
# requirements.lock pins exact transitive versions (constraints) for a reproducible
# release build; the package itself is resolved from pyproject.toml.
COPY app/ /app/
RUN pip install --no-cache-dir -c requirements.lock . \
    && mkdir -p /app/data \
    && chown -R smartbrain:smartbrain /app

# Stamp the release version into the image so /api/health and the UI report the real version no
# matter how the container is run. Placed AFTER the pip install (the expensive layer) so bumping
# the version only invalidates this cheap ENV and the trivial layers below it, never the deps.
ARG SMARTBRAIN_VERSION=0.0.0-dev
ENV SMARTBRAIN_VERSION=${SMARTBRAIN_VERSION}

USER smartbrain

EXPOSE 33000

# Linear, predictable startup. serve.py reads host/port/TLS from the env.
CMD ["python", "-m", "smartbrain_3000.serve"]
