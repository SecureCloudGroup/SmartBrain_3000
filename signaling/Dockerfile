# SmartBrain_3000 signaling broker — intentionally tiny (websockets only) so the
# one operator-run public node is easy to audit and cheap to run.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 sb && pip install --no-cache-dir "websockets==16.0"
USER sb
WORKDIR /srv
COPY server.py /srv/server.py

EXPOSE 8089
CMD ["python", "server.py"]
