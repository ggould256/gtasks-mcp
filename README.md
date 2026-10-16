NOTE:  This was fully authored by Claude Opus 5.5 as part of a
test.  It is not intended as an original work.

# gtasks-mcp

A remote MCP server that lets an AI agent create, read, update and delete the
user's Google Tasks and task lists.

The server is **stateless and holds no secrets**. The client agent does the
OAuth2 flow with Google itself and sends the resulting access token on every
request:

```http
Authorization: Bearer <google-access-token>
```

The server checks the token with Google's tokeninfo endpoint, uses it to call
the Tasks API for that one request, and discards it. Tokens are never written
to disk or logged. A successful verification is cached in memory for at most
60 seconds (and never past the token's expiry), so that a burst of tool calls
doesn't mean a burst of tokeninfo calls. Refreshing expired tokens is the
client's job: no refresh token or client secret ever reaches this server.

## Client requirements

The connecting agent needs:

- A Google Cloud project with the **Google Tasks API** enabled, and an OAuth
  client ID (and secret, for a web/confidential client) of its own.
- An access token granted the scope `https://www.googleapis.com/auth/tasks`.
- To send that token as a Bearer header on every MCP request, to
  `https://<host>/mcp` (Streamable HTTP transport).

Clients that follow the MCP authorization spec can discover the details
automatically. An unauthenticated request gets a `401` whose
`WWW-Authenticate` header points at
`/.well-known/oauth-protected-resource/mcp` (RFC 9728), which names
`https://accounts.google.com` as the authorization server and the Tasks scope
as required. Google does not support dynamic client registration, so the
client must be preconfigured with its Google client ID.

A token that is valid but lacks the Tasks scope gets a `403` with
`error="insufficient_scope"`.

## Running

```sh
uv sync
GTASKS_MCP_PUBLIC_URL=https://tasks.example.com/mcp uv run gtasks-mcp
```

Configuration is through environment variables. None of them are secret.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GTASKS_MCP_PUBLIC_URL` | *(required)* | Externally visible URL of the MCP endpoint. Its path is the path the endpoint is served on. |
| `GTASKS_MCP_HOST` | `127.0.0.1` | Interface to listen on. |
| `GTASKS_MCP_PORT` | `8000` | Port to listen on. |
| `GTASKS_MCP_ALLOWED_CLIENT_IDS` | *(any)* | Comma-separated Google OAuth client IDs whose tokens are accepted. Recommended in production, so tokens minted for unrelated apps are refused. |
| `GTASKS_MCP_ALLOWED_ORIGINS` | *(none)* | Comma-separated extra browser `Origin` values to accept (requests with no `Origin` header are always allowed). |
| `GTASKS_MCP_LOG_LEVEL` | `INFO` | At `DEBUG`, each request's method, path and header *names* are logged, which helps when working out what a client sends. Header values are never logged. |

The server speaks plain HTTP and is meant to sit behind a TLS-terminating
reverse proxy that forwards paths unchanged. Requests are only accepted with a
`Host` header matching `GTASKS_MCP_PUBLIC_URL` (or localhost), as protection
against DNS rebinding.

## Tools

Task lists:

- `list_task_lists()`
- `get_task_list(task_list_id)`
- `create_task_list(title)`
- `rename_task_list(task_list_id, title)`
- `delete_task_list(task_list_id)`: deletes the list and all its tasks.

Tasks (`@default` names the user's default list):

- `list_tasks(task_list_id, include_completed=False, due_min=None, due_max=None)`
- `get_task(task_list_id, task_id)`
- `create_task(task_list_id, title, notes=None, due=None, parent=None, previous=None)`
- `update_task(task_list_id, task_id, title=None, notes=None, due=None, status=None, clear_notes=False, clear_due=False)`
- `complete_task(task_list_id, task_id)` / `uncomplete_task(task_list_id, task_id)`
- `move_task(task_list_id, task_id, parent=None, previous=None, destination_task_list_id=None)`
- `delete_task(task_list_id, task_id)`
- `clear_completed(task_list_id)`: hides all completed tasks in a list.

Dates can be given as `YYYY-MM-DD` or RFC 3339. Google Tasks stores only the
date of a due value, never a time of day.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests make no network calls. The Google Tasks API is exercised through
the real discovery-built client over a recording fake HTTP layer, tokeninfo
through an `httpx2.MockTransport`, and the whole HTTP app through the MCP
client over an in-process ASGI transport.
