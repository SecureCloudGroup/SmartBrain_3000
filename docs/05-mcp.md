# Connect external tools (MCP)

SmartBrain_3000 is also an **MCP server** — it can expose your **Knowledge base
(read-only)** to a desktop AI client (e.g. Claude Desktop, Cursor). The
tool reads your knowledge to ground its answers; it can't change anything.

## Turn it on

Open **Settings → Connections (MCP)** and click **Generate token**. MCP is **off until a
token exists** — generating one enables it. The page then shows the endpoint and the token,
with **Copy token**, **Regenerate** (mints a new one and invalidates the old), and
**Revoke** (turns access off again). Managing the token is Desktop-only; a paired phone
can't read or change it.

**SmartBrain has to be unlocked.** The token authorizes the connection, but the knowledge
base is encrypted — while the app is locked, a client's calls are refused with *"SmartBrain
is locked; unlock it to use the knowledge base"*.

By default the endpoint is loopback-only:

```
http://localhost:33000/mcp/
```

Every request must include the token as a bearer header:

```
Authorization: Bearer <your-token>
```

## Point a tool at it

In your MCP client (Claude Desktop, Cursor, or another desktop AI app), add a server with the
endpoint and the `Authorization` header above. For a client that takes a streamable-HTTP
server as JSON, it looks like this (paste your token):

```json
{
  "mcpServers": {
    "smartbrain": {
      "url": "http://localhost:33000/mcp/",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

The client then sees exactly two tools:

| Tool | What it does |
| --- | --- |
| `kb_search` | Searches your knowledge by meaning, falling back to keyword search if no embedding model is available. Returns matching documents as id, title, snippet, and score. Takes a `limit`, 1 to 20, defaulting to 5. |
| `kb_read` | Returns one document in full, by the id `kb_search` gave back. |

A typical use is to ask the client a question and let it search your knowledge for the
grounding, the same way SmartBrain's own assistant does.

## What it can and can't do

- **Can:** search and read your Knowledge base. Content that came from an imported or
  subscribed vault is labeled with its provenance (which vault, whose key), so a client
  can treat third-party knowledge as data rather than instructions.
- **Can't:** see your credentials, write or delete anything, or reach other
  features. There is no tool to add, edit, rename, or delete a document, and none to
  reach Chat, Planner, Schedules, Email, or Settings. Vaults are not exposed either — a
  client sees documents, not the vault structure.
- **Where from:** by default it's reachable only from your own machine (loopback). It
  follows the app's host binding, so a LAN/HTTPS setup that exposes the app exposes it too.
  The token is stored encrypted at rest; revoke any time in Settings → Connections (MCP).

## Next

- [Backup & recovery](06-backup-recovery.md).
- [Privacy & security](07-privacy-security.md).
