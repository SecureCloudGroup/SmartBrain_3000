# Contributing to SmartBrain_3000

Thanks for your interest in improving SmartBrain_3000. This is a local-first,
single-user AI assistant that runs entirely on your own machine. On macOS and
Windows it installs and runs without Docker; Docker is the fallback on Linux and
Intel Macs. Development and the test suite still use containers — see below.

## License of contributions

SmartBrain_3000 is distributed under the **Elastic License 2.0** (see
[`LICENSE`](LICENSE)). By submitting a contribution (a pull request, patch, or
otherwise), you certify that you wrote it or otherwise have the right to submit
it, and you agree that your contribution is licensed under the same Elastic
License 2.0 as the rest of the project.

## Ground rules

The codebase follows a small, strict set of engineering rules — please follow
them in any PR. The headlines:

- **Simplicity first** — the minimum code that solves the problem; no speculative
  abstractions or configurability that wasn't asked for.
- **Surgical changes** — touch only what the change requires; match the
  surrounding style; don't refactor unrelated code.
- **NASA Power-of-10** — simple control flow (no recursion/`eval`), bounded loops,
  ≥2 assertions per function, functions ≤~60 lines, check all return values, no
  magic/deep-proxy patterns, **zero linter warnings**.

Security-sensitive areas (the key vault, the secret store, the tool/approval
gateway, OAuth, anything touching the network) get extra scrutiny — keep the
credential firewall intact: tools and external clients must never receive raw
secrets.

## Local development

CI (GitHub Actions) runs on every PR and on pushes to `main` — backend (ruff +
pytest), web (svelte-check + vitest + build), and a build + test of the shipped
Docker image; all three are **required checks** and must pass before a PR can merge.
Still build and verify locally first — you only need Docker (no host Python/Node
required).

**Backend — tests + lint** (throwaway container):

```bash
docker run --rm -v "$PWD/app":/app -w /app python:3.12-slim \
  bash -c "pip install -e '.[dev]' && ruff check && pytest -q"
```

**Frontend — type-check + build** (output is committed to
`app/smartbrain_3000/web` so the runtime image stays Python-only):

```bash
docker run --rm -v "$PWD/web":/web -v "$PWD/app":/app -w /web node:22-slim \
  bash -c "npm ci && npm run check && npm run build"
```

**Full stack** — bring it up and exercise it against a real model:

```bash
cd compose && docker compose up -d --build
# app on http://localhost:33000
```

**Version fields** — the `version` in `app/pyproject.toml` and `web/package.json` is not the
product version and is deliberately never bumped. The running version comes from the release
tag: baked into the Docker image as `SMARTBRAIN_VERSION`, written to the native install's
`VERSION` file, and compiled into the launcher via `-X main.launcherVersion`. `/api/health`
reports it and the UI displays it. Don't "fix" those two files.

## Pull requests

1. Branch from `main`.
2. Keep the diff focused; every changed line should trace to the stated goal.
3. Add or update tests; `ruff` and `svelte-check` must be clean (zero warnings).
4. If you committed a frontend change, rebuild the SPA so the committed output
   matches the source.
5. Describe what you changed and how you verified it.

## Reporting bugs and requesting features

Use the GitHub issue templates. For **security vulnerabilities**, do **not** open
a public issue — follow [`SECURITY.md`](SECURITY.md) instead.

## Contact

For licensing, security, and other direct inquiries to the maintainers, email
**info@securecloudgroup.com**.
