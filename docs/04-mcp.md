# Connect external tools (MCP)

SmartBrain_3000 is also an **MCP server** — it can expose your **Knowledge base
(read-only)** to a desktop AI client (e.g. Claude Desktop, Cursor). The
tool reads your knowledge to ground its answers; it can't change anything.

## Turn it on

Open **Settings → MCP** and **generate an access token**. MCP is **off until a
token exists** — generating one enables it; revoking it turns access off again.

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

The client can then call the read-only Knowledge tools (search and read your documents).

## What it can and can't do

- **Can:** search and read your Knowledge base.
- **Can't:** see your credentials, write or delete anything, or reach other
  features — and by default it's reachable only from your own machine (loopback); it
  follows the app's host binding, so a LAN/HTTPS setup that exposes the app exposes it
  too. The token is stored encrypted at rest; revoke any time in Settings → MCP.

## Next

- [Backup & recovery](05-backup-recovery.md).
- [Privacy & security](06-privacy-security.md).
