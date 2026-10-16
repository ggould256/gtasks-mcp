"""MCP server exposing Google Tasks operations to a client agent over Streamable HTTP."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

import anyio.to_thread
import uvicorn
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.types import ASGIApp, Receive, Scope, Send

from gtasks_mcp.auth import GOOGLE_ISSUER, TASKS_SCOPE, GoogleTokenVerifier, bearer_token_from_headers
from gtasks_mcp.client import TasksClient
from gtasks_mcp.config import ConfigError, Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

INSTRUCTIONS = (
    "Create, read, update and delete the user's Google Tasks. Every request must carry a Google "
    "OAuth access token with the https://www.googleapis.com/auth/tasks scope as "
    "'Authorization: Bearer <token>'. Call list_task_lists first to find task list IDs; "
    "'@default' also names the user's default list."
)

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
_IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


def build_server(
    settings: Settings,
    token_verifier: TokenVerifier | None = None,
    client_factory: Callable[[str], TasksClient] = TasksClient.for_token,
) -> MCPServer:
    """Create the MCP server, with every tool acting on the calling request's Google token."""
    mcp = MCPServer(
        "gtasks-mcp",
        instructions=INSTRUCTIONS,
        token_verifier=token_verifier or GoogleTokenVerifier(settings.allowed_client_ids),
        auth=AuthSettings(
            issuer_url=GOOGLE_ISSUER,
            resource_server_url=settings.resource_url,
            required_scopes=[TASKS_SCOPE],
        ),
        log_level=settings.log_level,
    )

    async def call(ctx: Context, operation: Callable[[TasksClient], T]) -> T:
        """Run a blocking Tasks API operation in a worker thread, with this request's token."""
        # On Streamable HTTP the request context carries the Starlette request for this message.
        request = ctx.request_context.request
        client = client_factory(bearer_token_from_headers(getattr(request, "headers", None)))
        return await anyio.to_thread.run_sync(operation, client)

    # Task lists.

    @mcp.tool(annotations=_READ)
    async def list_task_lists(ctx: Context) -> list[dict[str, Any]]:
        """List all of the user's task lists (id, title, updated)."""
        return await call(ctx, TasksClient.list_task_lists)

    @mcp.tool(annotations=_READ)
    async def get_task_list(task_list_id: str, ctx: Context) -> dict[str, Any]:
        """Get one task list by ID."""
        return await call(ctx, partial(TasksClient.get_task_list, task_list_id=task_list_id))

    @mcp.tool(annotations=_WRITE)
    async def create_task_list(title: str, ctx: Context) -> dict[str, Any]:
        """Create a new task list with the given title."""
        return await call(ctx, partial(TasksClient.create_task_list, title=title))

    @mcp.tool(annotations=_IDEMPOTENT_WRITE)
    async def rename_task_list(task_list_id: str, title: str, ctx: Context) -> dict[str, Any]:
        """Change a task list's title."""
        return await call(ctx, partial(TasksClient.rename_task_list, task_list_id=task_list_id, title=title))

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_task_list(task_list_id: str, ctx: Context) -> str:
        """Permanently delete a task list and every task in it."""
        await call(ctx, partial(TasksClient.delete_task_list, task_list_id=task_list_id))
        return "Deleted task list."

    # Tasks.

    @mcp.tool(annotations=_READ)
    async def list_tasks(
        task_list_id: str,
        ctx: Context,
        include_completed: bool = False,
        due_min: str | None = None,
        due_max: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks in a task list. Open tasks only unless `include_completed` is true.

        `due_min`/`due_max` optionally bound the due date ('YYYY-MM-DD' or RFC 3339).
        Subtasks have a `parent` field naming their parent task's ID.
        """
        return await call(
            ctx,
            partial(
                TasksClient.list_tasks,
                task_list_id=task_list_id,
                include_completed=include_completed,
                due_min=due_min,
                due_max=due_max,
            ),
        )

    @mcp.tool(annotations=_READ)
    async def get_task(task_list_id: str, task_id: str, ctx: Context) -> dict[str, Any]:
        """Get one task by ID."""
        return await call(ctx, partial(TasksClient.get_task, task_list_id=task_list_id, task_id=task_id))

    @mcp.tool(annotations=_WRITE)
    async def create_task(
        task_list_id: str,
        title: str,
        ctx: Context,
        notes: str | None = None,
        due: str | None = None,
        parent: str | None = None,
        previous: str | None = None,
    ) -> dict[str, Any]:
        """Create a task.

        `due` is a date ('YYYY-MM-DD' or RFC 3339); Google Tasks stores only the
        date, never a time of day. `parent` makes it a subtask of that task ID;
        `previous` places it after that sibling task ID (default: first).
        """
        return await call(
            ctx,
            partial(
                TasksClient.create_task,
                task_list_id=task_list_id,
                title=title,
                notes=notes,
                due=due,
                parent=parent,
                previous=previous,
            ),
        )

    @mcp.tool(annotations=_IDEMPOTENT_WRITE)
    async def update_task(
        task_list_id: str,
        task_id: str,
        ctx: Context,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
        clear_notes: bool = False,
        clear_due: bool = False,
    ) -> dict[str, Any]:
        """Change fields of a task; omitted fields are left unchanged.

        `status` is 'needsAction' or 'completed'. `due` is a date ('YYYY-MM-DD'
        or RFC 3339; only the date is kept). Set `clear_notes`/`clear_due` to
        remove the notes or due date. To move a task use move_task.
        """
        return await call(
            ctx,
            partial(
                TasksClient.update_task,
                task_list_id=task_list_id,
                task_id=task_id,
                title=title,
                notes=notes,
                due=due,
                status=status,
                clear_notes=clear_notes,
                clear_due=clear_due,
            ),
        )

    @mcp.tool(annotations=_IDEMPOTENT_WRITE)
    async def complete_task(task_list_id: str, task_id: str, ctx: Context) -> dict[str, Any]:
        """Mark a task as completed."""
        return await call(ctx, partial(TasksClient.complete_task, task_list_id=task_list_id, task_id=task_id))

    @mcp.tool(annotations=_IDEMPOTENT_WRITE)
    async def uncomplete_task(task_list_id: str, task_id: str, ctx: Context) -> dict[str, Any]:
        """Reopen a completed task (mark it as needing action)."""
        return await call(
            ctx, partial(TasksClient.uncomplete_task, task_list_id=task_list_id, task_id=task_id)
        )

    @mcp.tool(annotations=_WRITE)
    async def move_task(
        task_list_id: str,
        task_id: str,
        ctx: Context,
        parent: str | None = None,
        previous: str | None = None,
        destination_task_list_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a task within its list or to another list.

        `parent` makes it a subtask of that task ID (omit for top level);
        `previous` places it after that sibling (omit for first position);
        `destination_task_list_id` moves it to another list.
        """
        return await call(
            ctx,
            partial(
                TasksClient.move_task,
                task_list_id=task_list_id,
                task_id=task_id,
                parent=parent,
                previous=previous,
                destination_task_list_id=destination_task_list_id,
            ),
        )

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_task(task_list_id: str, task_id: str, ctx: Context) -> str:
        """Permanently delete a task."""
        await call(ctx, partial(TasksClient.delete_task, task_list_id=task_list_id, task_id=task_id))
        return "Deleted task."

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def clear_completed(task_list_id: str, ctx: Context) -> str:
        """Hide all completed tasks in a list, as 'Delete all completed tasks' does in Google's apps."""
        await call(ctx, partial(TasksClient.clear_completed, task_list_id=task_list_id))
        return "Cleared completed tasks."

    return mcp


class RequestLogMiddleware:
    """Logs each HTTP request's method, path and header names at DEBUG level.

    Meant for discovering what a client actually sends (discovery probes,
    headers). Header values are never logged, since Authorization carries the
    caller's Google token.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log the request (if DEBUG is enabled) and the response status, then pass it on."""
        if scope["type"] != "http" or not logger.isEnabledFor(logging.DEBUG):
            await self.app(scope, receive, send)
            return

        header_names = sorted({name.decode("latin-1").lower() for name, _ in scope.get("headers", [])})
        logger.debug("%s %s headers=%s", scope["method"], scope["path"], header_names)

        async def send_logging_status(message: Any) -> None:
            if message["type"] == "http.response.start":
                logger.debug("%s %s -> %d", scope["method"], scope["path"], message["status"])
            await send(message)

        await self.app(scope, receive, send_logging_status)


def build_app(
    settings: Settings,
    token_verifier: TokenVerifier | None = None,
    client_factory: Callable[[str], TasksClient] = TasksClient.for_token,
) -> ASGIApp:
    """Build the complete ASGI application: MCP endpoint, OAuth metadata, and request logging."""
    mcp = build_server(settings, token_verifier, client_factory)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[settings.public_host, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[
            settings.public_origin,
            *sorted(settings.allowed_origins),
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        # Stateless: each request stands alone with its own bearer token, and the
        # server keeps no per-client state between requests.
        stateless_http=True,
        json_response=True,
        transport_security=transport_security,
        host=settings.host,
    )
    return RequestLogMiddleware(app)


def main() -> None:
    """Run the MCP server over Streamable HTTP, configured from GTASKS_MCP_* variables."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"gtasks-mcp: {exc}")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # These libraries can log request URLs at INFO/DEBUG; keep them quiet.
    for noisy in ("httpx2", "httpcore2", "googleapiclient.discovery"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("Serving MCP at %s (listening on %s:%d)", settings.resource_url, settings.host, settings.port)
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )
