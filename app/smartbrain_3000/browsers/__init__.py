"""Browser components (Round 20, ni-format §35): standalone, replaceable, hash-pinned
headless browsers the app can load to read a page the way a browser does.

Each engine is described by a manifest shipped inside this package (``engines/<name>.json``).
Nothing here trusts the engine: it runs as a one-shot ``fetch`` subprocess under jail
hygiene, inside an OS wall, with its only network route through a key-less egress child
that enforces the SSRF guard, the site policy and the caps. A missing or failed wall
means ``unavailable``, never a render (fail closed).

Modules:

- ``manifest``   — the strict manifest schema and the argv token grammar.
- ``cmdline``    — value checks + the argv/env builder (closed placeholders, allowlisted
  flags, forbidden tokens golden-tested).
- ``release``    — pinned download (one allowlisted redirect, size cap, hash) + safe unpack.
- ``install``    — lifecycle: phases, commit/prune, self-test, ``verify_installed`` on
  every launch, Status rows.
- ``jail_egress``/``egress`` — the CONNECT-only egress child and its parent-side handle.
- ``walls``      — the confinement interface; B1 has no real wall (every platform
  reports ``Unavailable``), a test-only ``NullWall`` exists for the fake engine.
- ``proc``       — engine process hygiene: spawn, watchdogs, group kill, stderr tap.
- ``runner``     — one render: verify → wall → egress → engine → tripwires → cleanup.
- ``router``     — static tier (netguard) → render tier, need signals, circuit breaker.
"""
