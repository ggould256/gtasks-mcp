NOTE:  This was fully authored by Claude Opus 5.5 as part of a
test.  It is not intended as an original work.

# gtasks-mcp

A remote MCP server that lets an AI agent create, read, update and delete the
user's Google Tasks and task lists.

Every MCP request carries a Google OAuth access token for the Tasks scope:

```http
Authorization: Bearer <google-access-token>
```

The server checks the token with Google's tokeninfo endpoint, uses it to call
the Tasks API for that one request, and discards it. Tokens are never written
to disk or logged. A successful verification is cached in memory for at most
60 seconds (and never past the token's expiry), so that a burst of tool calls
doesn't mean a burst of tokeninfo calls. A token that is valid but lacks the
Tasks scope gets a `403` with `error="insufficient_scope"`.

Clients that follow the MCP authorization spec discover how to get a token
automatically: an unauthenticated request gets a `401` whose
`WWW-Authenticate` header points at `/.well-known/oauth-protected-resource/mcp`
(RFC 9728). What that names as the authorization server depends on the mode.

### Mode 1: clients bring their own Google token (no server secrets)

By default the metadata names `https://accounts.google.com` and the server
holds no secrets at all. The client needs its own Google Cloud OAuth client
(with the Google Tasks API enabled) and does the OAuth flow with Google
itself. Google does not support dynamic client registration, so this only
suits clients that can be preconfigured with a Google client ID.

### Mode 2: built-in sign-in broker (for clients that register themselves)

Many MCP clients insist on registering themselves with the authorization
server (RFC 7591 dynamic client registration). For those, give the server a
Google OAuth **web application** client (`GTASKS_MCP_GOOGLE_CLIENT_FILE`).
The server then acts as the authorization server clients see, offering
`/register`, `/authorize`, `/token` and `/revoke`, and signs users in with
Google:

1. The client registers at `/register` and sends the user to `/authorize`.
2. The server redirects the user to Google sign-in, which returns to
   `/oauth/google/callback`.
3. The client redeems its code (with PKCE) at `/token` and receives Google's
   own access token, plus a refresh token that only works through this server.

The Google client secret is the only secret involved. The server still
**stores nothing**: client registrations, sign-in state, authorization codes
and refresh tokens are encrypted and authenticated with a key derived from
that secret, and handed to the client to hold. A registration therefore
survives server restarts, and rotating the Google client secret invalidates
every registration and refresh token, so clients must reconnect.

Set up the Google client in the Google Cloud Console (APIs & Services →
Credentials → Create credentials → OAuth client ID → *Web application*), with
the authorized redirect URI `https://<host>/oauth/google/callback`, and
download its JSON. Keep the OAuth consent screen *in production* rather than
*testing*: in testing mode, Google expires refresh tokens after 7 days.

## Running

```sh
uv sync
GTASKS_MCP_PUBLIC_URL=https://tasks.example.com/mcp uv run gtasks-mcp
```

Configuration is through environment variables. None of them are secret; the
Google client file they may point to is.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GTASKS_MCP_PUBLIC_URL` | *(required)* | Externally visible URL of the MCP endpoint. Its path is the path the endpoint is served on. |
| `GTASKS_MCP_HOST` | `127.0.0.1` | Interface to listen on. |
| `GTASKS_MCP_PORT` | `8000` | Port to listen on. |
| `GTASKS_MCP_ALLOWED_CLIENT_IDS` | *(any)* | Comma-separated Google OAuth client IDs whose tokens are accepted. Recommended in production, so tokens minted for unrelated apps are refused. |
| `GTASKS_MCP_ALLOWED_ORIGINS` | *(none)* | Comma-separated browser `Origin` values allowed to call `/mcp` (CORS), beyond the public URL's own. Needed for clients that run in a browser. Requests with no `Origin` header are always allowed. |
| `GTASKS_MCP_GOOGLE_CLIENT_FILE` | *(none)* | Google OAuth web client JSON; enables the sign-in broker (mode 2). If unset, the systemd credential `google-client.json` is used when loaded (`LoadCredential=`). The broker's own client ID is automatically added to the allowed client IDs. |
| `GTASKS_MCP_LOG_LEVEL` | `INFO` | At `DEBUG`, each request's method, path, `Origin` and header *names* are logged, which helps when working out what a client sends. Other header values are never logged. |

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
