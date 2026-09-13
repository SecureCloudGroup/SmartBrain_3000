# Connect external tools (MCP)

SmartBrain_3000 is also an **MCP server** — it can expose your **Knowledge base
(read-only)** to a desktop AI client (e.g. Claude Desktop, Cursor). The
tool reads your knowledge to ground its answers; it can't change anything.

MCP runs in two directions here, and they are separate things: this page covers
the **inbound** server above first, then the **outbound** side — MCP servers
*you* run, feeding [Neural Interface](03-features.md#neural-interface) cards.

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

## Connect out: your MCP servers (for Neural Interface cards)

The other direction: SmartBrain can **consume** MCP servers as data sources for
[Neural Interface](03-features.md#neural-interface) cards — which is how database
cards work. Your own Postgres/SQLite MCP server keeps its credentials in its own
process; SmartBrain never holds the database password.

The rules, plainly:

- **Only servers you add explicitly.** The lower half of **Settings → Connections
  (MCP)** is where servers live — a label plus either a command SmartBrain launches
  each run (*stdio*) or a URL you host (*http*). Adding, editing, and removing them
  is Desktop-only. SmartBrain never reads ambient MCP config from disk (no
  `.mcp.json` discovery, ever), and no assistant tool can create or change a server.
- **Per card, one frozen tool call.** A card names one server, one tool, and the
  exact arguments — all shown in full on the approval card and frozen from then on.
  Changing any of it means asking you again. The server's tool listings and
  descriptions are never fed to a model; you pick the tool by name.
- **Credentials stay in your server.** A server config carries no secrets, and a
  card's spec can't either — connection strings, passwords, and keys live in your
  own server process, where SmartBrain never sees them.
- **The one address exception, stated honestly.** Everywhere else, SmartBrain's
  network guard refuses localhost and LAN addresses. An MCP server's address is the
  single deliberate exception — your own loopback or LAN server is the entire
  point. The address is typed by you, frozen afterwards, and unreachable by
  anything a model writes; redirects are refused so a reply can't steer the
  connection elsewhere.
- **Results are data, not instructions.** Whatever the server returns is treated
  like any other fetched content — bounded in size, checked against the card's
  validated data shape, and rendered as plain text.

Up to 10 servers can be configured. A server that's still referenced by a card
refuses deletion until the card is gone — so a live card can't lose its source
out from under it.

## Next

- [Backup & recovery](06-backup-recovery.md).
- [Privacy & security](07-privacy-security.md).
