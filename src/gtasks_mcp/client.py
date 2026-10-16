"""Thin wrapper around the Google Tasks API v1."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError

TASK_STATUSES = ("needsAction", "completed")

# Bookkeeping fields Google returns on every resource that only cost the agent tokens.
_NOISE_FIELDS = ("kind", "etag", "selfLink")

# Safety bound on pagination: 100 pages of 100 items each.
_MAX_PAGES = 100


class TasksError(ToolError):
    """A Google Tasks failure reported back to the calling agent as a tool error."""


def _http_error_message(exc: HttpError) -> str:
    """Describe a Google API HTTP error without echoing the raw response."""
    status = exc.resp.status if exc.resp is not None else None
    reason = exc.reason or "no details"
    match status:
        case 400:
            return f"Google Tasks rejected the request as invalid: {reason}"
        case 401:
            return "Google rejected the access token (expired or revoked); obtain a fresh token and retry."
        case 403:
            return f"Access denied by Google Tasks (check the token has the tasks scope): {reason}"
        case 404:
            return "Not found: no task list or task exists with the given ID."
        case 429:
            return "Google Tasks rate limit exceeded; retry later."
        case _:
            return f"Google Tasks request failed (HTTP {status}): {reason}"


def _clean(resource: dict[str, Any]) -> dict[str, Any]:
    """Drop bookkeeping fields from a Tasks API resource."""
    return {key: value for key, value in resource.items() if key not in _NOISE_FIELDS}


def _parse_date_or_datetime(value: str, field: str) -> date | datetime:
    """Parse 'YYYY-MM-DD' or an RFC 3339 timestamp, raising TasksError if it is neither."""
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        raise TasksError(
            f"{field} must be a date 'YYYY-MM-DD' or an RFC 3339 timestamp "
            f"such as '2026-08-27T00:00:00Z', got {value!r}"
        ) from None


def normalize_due(value: str) -> str:
    """Normalize a task due date to the form Google stores: 'YYYY-MM-DDT00:00:00.000Z'.

    Google Tasks keeps only the date of a due value; any time of day is
    discarded, so the date is taken exactly as written, without time-zone
    conversion.
    """
    parsed = _parse_date_or_datetime(value, "due")
    day = parsed.date() if isinstance(parsed, datetime) else parsed
    return f"{day.isoformat()}T00:00:00.000Z"


def normalize_timestamp(value: str, field: str) -> str:
    """Normalize a filter bound to an RFC 3339 UTC timestamp (a bare date means midnight UTC)."""
    parsed = _parse_date_or_datetime(value, field)
    if not isinstance(parsed, datetime):
        return f"{parsed.isoformat()}T00:00:00.000Z"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class TasksClient:
    """The Google Tasks operations this MCP server exposes, for a single caller's token."""

    def __init__(self, service: Any) -> None:
        """Wrap an already-built Tasks API service (see `for_token`)."""
        self._service = service

    @classmethod
    def for_token(cls, access_token: str) -> TasksClient:
        """Build a client that acts with the given Google OAuth access token.

        The credentials carry only the access token: no refresh token or client
        secret ever reaches this server, so refreshing is the client's job.
        """
        credentials = Credentials(token=access_token)
        service = build("tasks", "v1", credentials=credentials, cache_discovery=False)
        return cls(service)

    @staticmethod
    def _execute(request: Any) -> Any:
        """Execute an API request, translating Google errors into TasksError."""
        try:
            return request.execute()
        except HttpError as exc:
            raise TasksError(_http_error_message(exc)) from exc
        except RefreshError as exc:
            raise TasksError(
                "Google rejected the access token (expired or revoked); obtain a fresh token and retry."
            ) from exc

    def _paginate(self, make_request: Callable[[str | None], Any]) -> list[dict[str, Any]]:
        """Collect `items` across all pages of a list call."""
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._execute(make_request(page_token))
            items.extend(_clean(item) for item in page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return items

    # Task lists.

    def list_task_lists(self) -> list[dict[str, Any]]:
        """Return all of the user's task lists."""
        tasklists = self._service.tasklists()
        return self._paginate(lambda token: tasklists.list(maxResults=100, pageToken=token))

    def get_task_list(self, task_list_id: str) -> dict[str, Any]:
        """Return a single task list."""
        return _clean(self._execute(self._service.tasklists().get(tasklist=task_list_id)))

    def create_task_list(self, title: str) -> dict[str, Any]:
        """Create a new task list."""
        request = self._service.tasklists().insert(body={"title": title})
        return _clean(self._execute(request))

    def rename_task_list(self, task_list_id: str, title: str) -> dict[str, Any]:
        """Change a task list's title."""
        request = self._service.tasklists().patch(tasklist=task_list_id, body={"title": title})
        return _clean(self._execute(request))

    def delete_task_list(self, task_list_id: str) -> None:
        """Delete a task list and all the tasks in it."""
        self._execute(self._service.tasklists().delete(tasklist=task_list_id))

    # Tasks.

    def list_tasks(
        self,
        task_list_id: str,
        include_completed: bool = False,
        due_min: str | None = None,
        due_max: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the tasks in a task list, optionally including completed ones."""
        params: dict[str, Any] = {
            "tasklist": task_list_id,
            "maxResults": 100,
            # Tasks completed in Google's own apps are "hidden" as well as completed,
            # so both flags are needed to actually see completed tasks.
            "showCompleted": include_completed,
            "showHidden": include_completed,
        }
        if due_min is not None:
            params["dueMin"] = normalize_timestamp(due_min, "due_min")
        if due_max is not None:
            params["dueMax"] = normalize_timestamp(due_max, "due_max")
        tasks = self._service.tasks()
        return self._paginate(lambda token: tasks.list(pageToken=token, **params))

    def get_task(self, task_list_id: str, task_id: str) -> dict[str, Any]:
        """Return a single task."""
        return _clean(self._execute(self._service.tasks().get(tasklist=task_list_id, task=task_id)))

    def create_task(
        self,
        task_list_id: str,
        title: str,
        notes: str | None = None,
        due: str | None = None,
        parent: str | None = None,
        previous: str | None = None,
    ) -> dict[str, Any]:
        """Create a task, optionally as a subtask of `parent` and positioned after `previous`."""
        body: dict[str, Any] = {"title": title}
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = normalize_due(due)
        params: dict[str, Any] = {"tasklist": task_list_id, "body": body}
        if parent is not None:
            params["parent"] = parent
        if previous is not None:
            params["previous"] = previous
        return _clean(self._execute(self._service.tasks().insert(**params)))

    def update_task(
        self,
        task_list_id: str,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
        clear_notes: bool = False,
        clear_due: bool = False,
    ) -> dict[str, Any]:
        """Update the given fields of a task, leaving unspecified fields unchanged."""
        if clear_notes and notes is not None:
            raise TasksError("Pass either notes or clear_notes, not both.")
        if clear_due and due is not None:
            raise TasksError("Pass either due or clear_due, not both.")

        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        elif clear_notes:
            body["notes"] = None
        if due is not None:
            body["due"] = normalize_due(due)
        elif clear_due:
            body["due"] = None
        if status is not None:
            if status not in TASK_STATUSES:
                raise TasksError(f"status must be one of {', '.join(TASK_STATUSES)}, got {status!r}")
            body["status"] = status
            if status == "needsAction":
                # Reopening a task requires clearing its completion timestamp too.
                body["completed"] = None
        if not body:
            raise TasksError("Nothing to update: pass at least one field to change.")

        request = self._service.tasks().patch(tasklist=task_list_id, task=task_id, body=body)
        return _clean(self._execute(request))

    def complete_task(self, task_list_id: str, task_id: str) -> dict[str, Any]:
        """Mark a task as completed."""
        return self.update_task(task_list_id, task_id, status="completed")

    def uncomplete_task(self, task_list_id: str, task_id: str) -> dict[str, Any]:
        """Mark a completed task as needing action again."""
        return self.update_task(task_list_id, task_id, status="needsAction")

    def move_task(
        self,
        task_list_id: str,
        task_id: str,
        parent: str | None = None,
        previous: str | None = None,
        destination_task_list_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a task: under another parent, after a sibling, and/or into another list."""
        params: dict[str, Any] = {"tasklist": task_list_id, "task": task_id}
        if parent is not None:
            params["parent"] = parent
        if previous is not None:
            params["previous"] = previous
        if destination_task_list_id is not None:
            params["destinationTasklist"] = destination_task_list_id
        return _clean(self._execute(self._service.tasks().move(**params)))

    def delete_task(self, task_list_id: str, task_id: str) -> None:
        """Delete a task."""
        self._execute(self._service.tasks().delete(tasklist=task_list_id, task=task_id))

    def clear_completed(self, task_list_id: str) -> None:
        """Hide all completed tasks in a list (they stop appearing in Google's own apps)."""
        self._execute(self._service.tasks().clear(tasklist=task_list_id))
