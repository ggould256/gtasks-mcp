"""Test doubles for the Google Tasks API HTTP layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httplib2
from googleapiclient.discovery import build

from gtasks_mcp.client import TasksClient


@dataclass
class RecordedRequest:
    """One HTTP request the Tasks API client sent."""

    method: str
    uri: str
    body: Any

    @property
    def path(self) -> str:
        """The request path, e.g. '/tasks/v1/lists/L1/tasks'."""
        return unquote(urlparse(self.uri).path)

    @property
    def query(self) -> dict[str, str]:
        """Query parameters, one value each."""
        return {key: values[0] for key, values in parse_qs(urlparse(self.uri).query).items()}


class RecordingHttp:
    """Stands in for httplib2.Http: records requests and replays queued (status, body) responses."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        """Queue responses to return in order; bodies are JSON-encoded (None means empty)."""
        self._responses = list(responses)
        self.requests: list[RecordedRequest] = []

    def request(self, uri, method="GET", body=None, headers=None, redirections=None, connection_type=None):
        """Record the request and return the next queued response."""
        self.requests.append(RecordedRequest(method, uri, json.loads(body) if body else None))
        if not self._responses:
            raise AssertionError(f"unexpected request: {method} {uri}")
        status, payload = self._responses.pop(0)
        content = b"" if payload is None else json.dumps(payload).encode()
        return httplib2.Response({"status": str(status), "content-type": "application/json"}), content


def fake_client(*responses: tuple[int, Any]) -> tuple[TasksClient, RecordingHttp]:
    """A TasksClient backed by the real discovery-built service over a RecordingHttp."""
    http = RecordingHttp(*responses)
    return TasksClient(build("tasks", "v1", http=http)), http


def google_error(status: int, message: str) -> tuple[int, Any]:
    """A response shaped like a Google API error."""
    return status, {"error": {"code": status, "message": message, "errors": [{"message": message}]}}
