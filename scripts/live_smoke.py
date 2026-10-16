"""Live end-to-end smoke test of a running gtasks-mcp server against real Google Tasks.

Plays the part of the client agent: mints a fresh Google access token from an
authorized-user JSON (refresh token + client ID/secret, kept client-side only),
then drives the server over Streamable HTTP with that token as a Bearer header.

All writes happen inside two scratch task lists that are created and deleted
by this script; existing lists are only read.

    uv run python scripts/live_smoke.py [--url URL] [--token-file PATH] [--server-log PATH]
    uv run python scripts/live_smoke.py --expect-rejected   # e.g. against a server whose
                                                            # allowlist excludes this client
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "gtasks-mcp" / "token.json"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "live-smoke", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class Checks:
    """Collects PASS/FAIL results."""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
        if not ok:
            self.failures += 1
        return ok


async def mint_access_token(token_file: Path) -> str:
    """Exchange the file's refresh token for a fresh access token (client-side only)."""
    data = json.loads(token_file.read_text())
    async with httpx2.AsyncClient(timeout=15) as http:
        response = await http.post(
            data.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "client_id": data["client_id"],
                "client_secret": data["client_secret"],
            },
        )
    if response.status_code != 200:
        error = response.json().get("error", "unknown") if response.content else "unknown"
        sys.exit(f"Could not refresh the access token: HTTP {response.status_code} {error}")
    return response.json()["access_token"]


def payload(result: Any) -> Any:
    """The structured value a tool returned (lists come wrapped as {'result': [...]})."""
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content


async def check_rejections(url: str, checks: Checks) -> None:
    """Requests without a valid token must get a 401 Bearer challenge."""
    async with httpx2.AsyncClient(timeout=15) as http:
        for label, headers in (
            ("no token -> 401 challenge", {}),
            ("bogus token -> 401", {"authorization": "Bearer not-a-real-token"}),
        ):
            response = await http.post(url, json=INITIALIZE, headers={**MCP_HEADERS, **headers})
            challenge = response.headers.get("www-authenticate", "")
            checks.check(
                label,
                response.status_code == 401 and "resource_metadata=" in challenge,
                f"HTTP {response.status_code}, www-authenticate={challenge!r}",
            )


async def expect_rejected(url: str, token: str, checks: Checks) -> None:
    """A real token must be refused (used against a server whose allowlist excludes it)."""
    async with httpx2.AsyncClient(timeout=15) as http:
        response = await http.post(
            url, json=INITIALIZE, headers={**MCP_HEADERS, "authorization": f"Bearer {token}"}
        )
    checks.check(
        "real token refused by allowlist -> 401", response.status_code == 401, f"HTTP {response.status_code}"
    )


async def exercise_tools(url: str, token: str, checks: Checks) -> None:
    """Full CRUD round trip, confined to scratch lists."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    http = httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=30)
    async with http, Client(streamable_http_client(url, http_client=http)) as client:

        async def call(name: str, **arguments: Any) -> Any:
            result = await client.call_tool(name, arguments)
            if result.is_error:
                text = result.content[0].text if result.content else ""
                raise RuntimeError(f"{name} failed: {text}")
            return payload(result)

        tools = {tool.name for tool in (await client.list_tools()).tools}
        checks.check("list_tools returns 14 tools", len(tools) == 14, str(sorted(tools)))

        lists = await call("list_task_lists")
        checks.check(
            "list_task_lists (read-only)", isinstance(lists, list) and len(lists) >= 1, repr(lists)[:200]
        )

        scratch_ids: list[str] = []
        try:
            first = await call("create_task_list", title=f"gtasks-mcp smoke {stamp} A")
            scratch_ids.append(first["id"])
            second = await call("create_task_list", title=f"gtasks-mcp smoke {stamp} B")
            scratch_ids.append(second["id"])
            checks.check("create_task_list x2", bool(first.get("id") and second.get("id")))

            renamed = await call(
                "rename_task_list", task_list_id=first["id"], title=f"gtasks-mcp smoke {stamp} A renamed"
            )
            fetched_list = await call("get_task_list", task_list_id=first["id"])
            checks.check(
                "rename_task_list + get_task_list",
                renamed["title"].endswith("renamed") and fetched_list["title"] == renamed["title"],
            )

            task = await call(
                "create_task",
                task_list_id=first["id"],
                title="Smoke parent",
                notes="some notes",
                due="2026-12-25",
            )
            checks.check(
                "create_task with notes and due",
                task.get("notes") == "some notes" and task.get("due", "").startswith("2026-12-25"),
                repr(task),
            )

            sub = await call("create_task", task_list_id=first["id"], title="Smoke child", parent=task["id"])
            checks.check("create_task as subtask", sub.get("parent") == task["id"], repr(sub))

            got = await call("get_task", task_list_id=first["id"], task_id=task["id"])
            checks.check("get_task", got["title"] == "Smoke parent")

            open_tasks = await call("list_tasks", task_list_id=first["id"])
            checks.check(
                "list_tasks shows both",
                {t["id"] for t in open_tasks} == {task["id"], sub["id"]},
                repr(open_tasks),
            )

            due_filtered = await call(
                "list_tasks", task_list_id=first["id"], due_min="2026-12-01", due_max="2026-12-31"
            )
            checks.check(
                "list_tasks due filter", [t["id"] for t in due_filtered] == [task["id"]], repr(due_filtered)
            )

            updated = await call(
                "update_task", task_list_id=first["id"], task_id=task["id"], title="Smoke parent (edited)"
            )
            checks.check("update_task title", updated["title"] == "Smoke parent (edited)")

            cleared = await call(
                "update_task", task_list_id=first["id"], task_id=task["id"], clear_notes=True, clear_due=True
            )
            checks.check(
                "update_task clear notes/due", "notes" not in cleared and "due" not in cleared, repr(cleared)
            )

            done = await call("complete_task", task_list_id=first["id"], task_id=sub["id"])
            checks.check("complete_task", done["status"] == "completed" and "completed" in done)

            visible = await call("list_tasks", task_list_id=first["id"])
            with_done = await call("list_tasks", task_list_id=first["id"], include_completed=True)
            checks.check(
                "completed hidden by default, shown with include_completed",
                sub["id"] not in {t["id"] for t in visible} and sub["id"] in {t["id"] for t in with_done},
            )

            reopened = await call("uncomplete_task", task_list_id=first["id"], task_id=sub["id"])
            checks.check(
                "uncomplete_task",
                reopened["status"] == "needsAction" and "completed" not in reopened,
                repr(reopened),
            )

            mover = await call("create_task", task_list_id=first["id"], title="Smoke mover")
            await call(
                "move_task",
                task_list_id=first["id"],
                task_id=mover["id"],
                destination_task_list_id=second["id"],
            )
            in_second = await call("list_tasks", task_list_id=second["id"])
            checks.check(
                "move_task to another list",
                [t["title"] for t in in_second] == ["Smoke mover"],
                repr(in_second),
            )

            await call("complete_task", task_list_id=first["id"], task_id=sub["id"])
            await call("clear_completed", task_list_id=first["id"])
            after_clear = await call("list_tasks", task_list_id=first["id"])
            checks.check("clear_completed", sub["id"] not in {t["id"] for t in after_clear})

            await call("delete_task", task_list_id=first["id"], task_id=task["id"])
            after_delete = await call("list_tasks", task_list_id=first["id"], include_completed=True)
            checks.check(
                "delete_task", task["id"] not in {t["id"] for t in after_delete if not t.get("deleted")}
            )

            missing = await client.call_tool(
                "get_task", {"task_list_id": first["id"], "task_id": "no-such-task"}
            )
            text = missing.content[0].text if missing.content else ""
            checks.check(
                "bad task id -> 'Not found' tool error",
                missing.is_error and ("Not found" in text or "invalid" in text),
                text,
            )
        finally:
            for list_id in scratch_ids:
                try:
                    await call("delete_task_list", task_list_id=list_id)
                except Exception as exc:  # report but keep cleaning up
                    checks.check(f"cleanup delete_task_list {list_id}", False, str(exc))
            remaining = {item["id"] for item in await call("list_task_lists")}
            checks.check("scratch lists deleted", not (remaining & set(scratch_ids)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--server-log", type=Path, help="server log to scan for token leaks")
    parser.add_argument(
        "--expect-rejected", action="store_true", help="only check that this client's real token is refused"
    )
    args = parser.parse_args()

    async def run() -> int:
        checks = Checks()
        token = await mint_access_token(args.token_file)
        print("PASS  minted fresh access token from refresh token")
        if args.expect_rejected:
            await expect_rejected(args.url, token, checks)
        else:
            await check_rejections(args.url, checks)
            await exercise_tools(args.url, token, checks)
        if args.server_log:
            log = args.server_log.read_text()
            checks.check("access token absent from server log", token not in log)
        print(f"\n{checks.failures} failure(s)")
        return 1 if checks.failures else 0

    sys.exit(anyio.run(run))


if __name__ == "__main__":
    main()
