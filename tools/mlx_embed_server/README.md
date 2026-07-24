# SmartBrain MLX embeddings server

A tiny OpenAI-compatible `/v1/embeddings` host service for the **MLX-only stack**.

Chat-oriented MLX servers (oMLX) serve only *encoder* embedders (BERT/BGE-M3) and refuse
decoder embedding models — verified live with Qwen3-Embedding ("not an embedding model").
This server uses `mlx-embeddings` (correct last-token pooling for Qwen3) and pairs with
the app's **MLX embeddings** provider (Settings → Local models).

## Install as a login service

```sh
./install.sh          # venv at ~/.smartbrain/mlx-embed + LaunchAgent on port 8899
```

First start downloads the model (~500 MB). Then in SmartBrain:

1. Settings → **Local models** → MLX embeddings → Connect (it auto-detects port 8899).
2. Settings → **Model routing** → Embedding → `mlxe/qwen3-embedding-0.6b`.
3. Knowledge → **Reindex** (full re-embed under the new model).

Environment overrides for `install.sh`: `SB_MLXE_MODEL` (default
`mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ`), `SB_MLXE_PORT` (default 8899).

## Uninstall

```sh
launchctl unload ~/Library/LaunchAgents/com.securecloudgroup.smartbrain.mlx-embed.plist
rm -rf ~/.smartbrain/mlx-embed ~/Library/LaunchAgents/com.securecloudgroup.smartbrain.mlx-embed.plist
```
