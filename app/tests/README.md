# Running the tests

Tests run **inside the `smartbrain_3000` container** (the host has no venv). Dev deps:

```sh
docker exec smartbrain_3000 pip install -q pytest ruff
```

## Standard run

```sh
docker exec -e SMARTBRAIN_ALLOWED_HOSTS=testserver,localhost,127.0.0.1 -w /app smartbrain_3000 \
  python -m pytest -q
```

This skips `test_signaling.py` (9 tests) because the WebRTC broker (`signaling/`) is not
shipped in the app image. **A skip here is silent — do not treat the standard run as a full
gate.** Use the release-gate run below.

## Release-gate run (everything, signaling included)

Mount the broker and require it (so the 9 signaling tests can never silently skip):

```sh
docker exec smartbrain_3000 mkdir -p /tmp/sig
docker cp signaling/server.py smartbrain_3000:/tmp/sig/server.py
docker exec -e PYTHONPATH=/tmp/sig -e SMARTBRAIN_REQUIRE_SIGNALING_TESTS=1 \
  -e SMARTBRAIN_ALLOWED_HOSTS=testserver,localhost,127.0.0.1 -w /app smartbrain_3000 \
  python -m pytest -q
```

`SMARTBRAIN_REQUIRE_SIGNALING_TESTS=1` makes a missing broker a hard error instead of a skip.
Expected: **0 skipped**.

## Real-boundary integration tests

`test_integration_*.py` + `_fakegateway.py` run our REAL code (gateway, agent, egress, KB,
OAuth, Gmail, scheduler) against REAL local servers over REAL sockets — no monkeypatching of
our own I/O. These exist because mocking our own functions once let two fully-broken features
ship past a green suite. **When you add an outbound HTTP call, add a real-boundary test that
asserts what goes on the wire** — a unit test that patches the function cannot catch a
dropped field or a misused client.
