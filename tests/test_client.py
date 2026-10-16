"""Tests for TasksClient against a recorded fake of the Google Tasks HTTP API."""

from __future__ import annotations

import pytest
from fakes import fake_client, google_error

from gtasks_mcp.client import TasksError, normalize_due, normalize_timestamp


def test_list_task_lists_follows_pagination_and_strips_noise():
    client, http = fake_client(
        (
            200,
            {
                "items": [{"id": "L1", "title": "One", "kind": "tasks#taskList", "etag": "x"}],
                "nextPageToken": "page2",
            },
        ),
        (200, {"items": [{"id": "L2", "title": "Two", "selfLink": "https://..."}]}),
    )

    lists = client.list_task_lists()

    assert lists == [{"id": "L1", "title": "One"}, {"id": "L2", "title": "Two"}]
    assert len(http.requests) == 2
    assert "pageToken" not in http.requests[0].query
    assert http.requests[1].query["pageToken"] == "page2"
    assert http.requests[0].path == "/tasks/v1/users/@me/lists"


def test_list_tasks_hides_completed_by_default():
    client, http = fake_client((200, {"items": [{"id": "T1"}]}))

    assert client.list_tasks("L1") == [{"id": "T1"}]

    query = http.requests[0].query
    assert http.requests[0].path == "/tasks/v1/lists/L1/tasks"
    assert query["showCompleted"] == "false"
    assert query["showHidden"] == "false"


def test_list_tasks_include_completed_and_due_bounds():
    client, http = fake_client((200, {}))

    assert (
        client.list_tasks(
            "L1", include_completed=True, due_min="2026-08-01", due_max="2026-08-31T12:00:00-07:00"
        )
        == []
    )

    query = http.requests[0].query
    assert query["showCompleted"] == "true"
    assert query["showHidden"] == "true"
    assert query["dueMin"] == "2026-08-01T00:00:00.000Z"
    assert query["dueMax"] == "2026-08-31T19:00:00.000Z"


def test_get_task():
    client, http = fake_client((200, {"id": "T1", "title": "Milk", "etag": "e"}))

    assert client.get_task("L1", "T1") == {"id": "T1", "title": "Milk"}
    assert http.requests[0].method == "GET"
    assert http.requests[0].path == "/tasks/v1/lists/L1/tasks/T1"


def test_create_task_sends_body_and_position():
    client, http = fake_client((200, {"id": "T2"}))

    client.create_task("L1", "Buy milk", notes="2%", due="2026-08-27T15:30:00Z", parent="T1", previous="T0")

    request = http.requests[0]
    assert request.method == "POST"
    assert request.path == "/tasks/v1/lists/L1/tasks"
    assert request.body == {"title": "Buy milk", "notes": "2%", "due": "2026-08-27T00:00:00.000Z"}
    assert request.query["parent"] == "T1"
    assert request.query["previous"] == "T0"


def test_create_task_minimal():
    client, http = fake_client((200, {"id": "T2"}))

    client.create_task("L1", "Buy milk")

    assert http.requests[0].body == {"title": "Buy milk"}
    assert http.requests[0].query == {"alt": "json"}


def test_update_task_patches_only_given_fields():
    client, http = fake_client((200, {"id": "T1"}))

    client.update_task("L1", "T1", title="New title", due="2026-09-01")

    request = http.requests[0]
    assert request.method == "PATCH"
    assert request.path == "/tasks/v1/lists/L1/tasks/T1"
    assert request.body == {"title": "New title", "due": "2026-09-01T00:00:00.000Z"}


def test_update_task_clears_fields_with_null():
    client, http = fake_client((200, {"id": "T1"}))

    client.update_task("L1", "T1", clear_notes=True, clear_due=True)

    assert http.requests[0].body == {"notes": None, "due": None}


def test_uncomplete_clears_completion_timestamp():
    client, http = fake_client((200, {"id": "T1"}))

    client.uncomplete_task("L1", "T1")

    assert http.requests[0].body == {"status": "needsAction", "completed": None}


def test_complete_task():
    client, http = fake_client((200, {"id": "T1", "status": "completed"}))

    assert client.complete_task("L1", "T1")["status"] == "completed"
    assert http.requests[0].body == {"status": "completed"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "Nothing to update"),
        ({"notes": "x", "clear_notes": True}, "either notes or clear_notes"),
        ({"due": "2026-01-01", "clear_due": True}, "either due or clear_due"),
        ({"status": "done"}, "status must be one of"),
        ({"due": "next tuesday"}, "due must be a date"),
    ],
)
def test_update_task_rejects_bad_arguments_without_calling_google(kwargs, message):
    client, http = fake_client()

    with pytest.raises(TasksError, match=message):
        client.update_task("L1", "T1", **kwargs)
    assert http.requests == []


def test_move_task():
    client, http = fake_client((200, {"id": "T1"}))

    client.move_task("L1", "T1", parent="P", previous="S", destination_task_list_id="L2")

    request = http.requests[0]
    assert request.method == "POST"
    assert request.path == "/tasks/v1/lists/L1/tasks/T1/move"
    assert request.query["parent"] == "P"
    assert request.query["previous"] == "S"
    assert request.query["destinationTasklist"] == "L2"


def test_delete_and_clear():
    client, http = fake_client((204, None), (204, None))

    client.delete_task("L1", "T1")
    client.clear_completed("L1")

    assert [(r.method, r.path) for r in http.requests] == [
        ("DELETE", "/tasks/v1/lists/L1/tasks/T1"),
        ("POST", "/tasks/v1/lists/L1/clear"),
    ]


def test_task_list_crud():
    client, http = fake_client(
        (200, {"id": "L9", "title": "New"}),
        (200, {"id": "L9", "title": "Renamed"}),
        (200, {"id": "L9", "title": "Renamed"}),
        (204, None),
    )

    assert client.create_task_list("New")["id"] == "L9"
    assert client.rename_task_list("L9", "Renamed")["title"] == "Renamed"
    assert client.get_task_list("L9")["title"] == "Renamed"
    client.delete_task_list("L9")

    assert [(r.method, r.path, r.body) for r in http.requests] == [
        ("POST", "/tasks/v1/users/@me/lists", {"title": "New"}),
        ("PATCH", "/tasks/v1/users/@me/lists/L9", {"title": "Renamed"}),
        ("GET", "/tasks/v1/users/@me/lists/L9", None),
        ("DELETE", "/tasks/v1/users/@me/lists/L9", None),
    ]


@pytest.mark.parametrize(
    "status, message",
    [
        (400, "rejected the request as invalid: Bad due"),
        (401, "rejected the access token"),
        (403, "Access denied"),
        (404, "Not found"),
        (429, "rate limit"),
        (500, "HTTP 500"),
    ],
)
def test_google_errors_become_tasks_errors(status, message):
    client, _ = fake_client(google_error(status, "Bad due"))

    with pytest.raises(TasksError, match=message):
        client.get_task("L1", "T1")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-08-27", "2026-08-27T00:00:00.000Z"),
        ("2026-08-27T00:00:00Z", "2026-08-27T00:00:00.000Z"),
        ("2026-08-27T23:30:00-07:00", "2026-08-27T00:00:00.000Z"),  # date as written, no tz shift
        ("2026-08-27T09:00:00", "2026-08-27T00:00:00.000Z"),
    ],
)
def test_normalize_due(value, expected):
    assert normalize_due(value) == expected


def test_normalize_timestamp_converts_to_utc():
    assert normalize_timestamp("2026-08-27T23:30:00-07:00", "x") == "2026-08-28T06:30:00.000Z"
    assert normalize_timestamp("2026-08-27T23:30:00", "x") == "2026-08-27T23:30:00.000Z"
