# Connect a model

SmartBrain_3000 talks to language models through a local **gateway** (Bifrost),
which runs as part of the stack. You can use **cloud providers** (with your own
API keys) and/or **local models** running on your machine. Nothing is sent to a
provider unless you configure it and use it.

## Cloud providers (your API keys)

Open **Settings → Providers** and add a key for any of:

- **OpenAI**
- **Anthropic**
- **Google (Gemini)**

![Settings → Cloud providers, with key fields for OpenAI, Anthropic, and Google](assets/02-providers.png)

Keys are stored **encrypted on your machine** and pushed to the local gateway
while you're unlocked; locking removes them from the gateway again. The app never
returns a stored key over its API — only the fact that one is set.

> Using a cloud model means your prompts (and any content you send) go to that
> provider. If you'd rather keep everything on your machine, use a local model.

## Local models (on your machine)

Local models run on the **host** (not inside the container), and the app reaches
them at `host.docker.internal`. Open **Settings → Local models** to connect them.

- **Ollama** — works on any OS. Install it, pull a model
  (e.g. `ollama pull llama3.1:8b`), and point SmartBrain_3000 at it.
- **MLX** — Apple-Silicon Macs only, run on the host.

The panel shows whether each is reachable and which models it has.

> **Already running Ollama?** You usually don't need to touch this panel.
> SmartBrain detects a local server on its default port and offers a one-tap
> **Connect** — on the **Chat** screen when you have no model yet, and here under
> the port field. The manual port/URL fields are for non-standard setups.

![Settings → Local models showing a detected Ollama server with a Connect link](assets/03-local-models.png)

## Embeddings (for Knowledge search)

Semantic search in the [Knowledge base](03-features.md) needs an **embedding
model**. The default is a **local** Ollama model — `nomic-embed-text:v1.5` — so
your knowledge content stays on-box.

**The installer pulls this for you** when Ollama is present (and
`python3 installer/install.py doctor` offers to). If you ever need to do it by hand,
pull that exact tag:

```sh
ollama pull nomic-embed-text:v1.5
```

The tag matters: the bare `nomic-embed-text` won't resolve. If semantic search shows
keyword results and says *"degraded"*, this model isn't pulled — run the command above
and **Reindex**. You can change the model, but pointing embeddings at a cloud provider
sends your documents there on every reindex — only do that if you accept that tradeoff.

## Next

- [Using SmartBrain_3000](03-features.md) — start chatting and add knowledge.
- [Connect external tools](04-mcp.md) — let a desktop AI client (e.g. Claude Desktop) read your Knowledge.
