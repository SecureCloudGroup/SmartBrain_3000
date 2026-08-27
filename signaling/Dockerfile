# SmartBrain_3000 signaling broker — intentionally tiny (websockets + cryptography) so the
# one operator-run public node is easy to audit and cheap to run. cryptography is the
# Ed25519 verifier for desktop proof-of-possession. /state holds the persisted desktop
# bindings (SIGNALING_STATE_FILE) and must be writable by the unprivileged user.
FROM python:3.14-slim

RUN useradd --create-home --uid 10001 sb && mkdir -p /state && chown sb /state \
    && pip install --no-cache-dir "websockets==16.0" "cryptography==50.0.1"
USER sb
WORKDIR /srv
COPY server.py /srv/server.py
COPY health.py /srv/health.py

EXPOSE 8089
CMD ["python", "server.py"]
