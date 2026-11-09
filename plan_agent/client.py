"""HTTP client for the Projekt_managment API.

* Credentials come from Settings (environment variables), are sent only in the
  Authorization header, and never appear in exceptions, reports or logs.
* Network errors, 429 and 5xx are retried with exponential backoff.
* 401/403 raise AuthError immediately (retrying would not help).
* Before any write the client loads GET /api/agent/schema and refuses to send
  fields the backend does not declare as writable.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from .config import Settings

log = logging.getLogger("plan_agent.client")


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class AuthError(ApiError):
    pass


class NetworkError(ApiError):
    pass


class DuplicateError(ApiError):
    @property
    def existing_ids(self) -> list:
        return self.body.get("existing_ids", [])


class VersionConflict(ApiError):
    pass


class DashboardClient:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None, sleep=time.sleep):
        if not settings.api_url:
            raise ApiError("PM_API_URL is not configured")
        self.base = settings.api_url
        self.s = session or requests.Session()
        self.timeout = settings.timeout
        self.max_retries = settings.max_retries
        self._sleep = sleep
        self._key = settings.api_key
        self._schema: Optional[dict] = None

    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, json_body: Any = None, auth: bool = True) -> dict:
        headers = {"Accept": "application/json"}
        if auth:
            if not self._key:
                raise AuthError("PM_AGENT_API_KEY is not configured")
            headers["Authorization"] = f"Bearer {self._key}"
        url = self.base + path
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.s.request(method, url, json=json_body, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise NetworkError(f"{method} {path}: network error after {attempt + 1} attempts "
                                       f"({type(exc).__name__})") from None
                log.warning("%s %s: %s, retrying in %.0fs", method, path, type(exc).__name__, delay)
                self._sleep(delay)
                delay *= 2
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                log.warning("%s %s: HTTP %s, retrying in %.0fs", method, path, resp.status_code, delay)
                self._sleep(delay)
                delay *= 2
                continue
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                raise ApiError(f"{method} {path}: invalid JSON response (HTTP {resp.status_code})",
                               resp.status_code) from None
            if not isinstance(body, dict):
                raise ApiError(f"{method} {path}: unexpected response shape", resp.status_code)
            if resp.status_code in (401, 403):
                raise AuthError(f"{method} {path}: authentication rejected (HTTP {resp.status_code})",
                                resp.status_code, body)
            if resp.status_code == 409 and body.get("error") == "duplicate":
                raise DuplicateError(body.get("message", "duplicate"), 409, body)
            if resp.status_code == 409 and body.get("error") == "version_conflict":
                raise VersionConflict(body.get("message", "version conflict"), 409, body)
            if resp.status_code >= 400:
                raise ApiError(f"{method} {path}: HTTP {resp.status_code} {body.get('error', '')}: "
                               f"{body.get('message', '')}", resp.status_code, body)
            return body
        raise NetworkError(f"{method} {path}: gave up")  # pragma: no cover

    # ------------------------------------------------------------------ #
    def schema(self) -> dict:
        if self._schema is None:
            body = self._request("GET", "/api/agent/schema")
            if not isinstance(body.get("writable_fields"), list):
                raise ApiError("GET /api/agent/schema: response has no writable_fields list")
            self._schema = body
        return self._schema

    def writable_fields(self) -> set[str]:
        return set(self.schema()["writable_fields"])

    def _check_fields(self, data: dict) -> None:
        unsupported = set(data) - self.writable_fields()
        if unsupported:
            raise ApiError(f"refusing to send fields the backend does not support: {sorted(unsupported)}")

    def list_projects(self) -> list[dict]:
        body = self._request("GET", "/api/agent/projects")
        projects = body.get("projects")
        if not isinstance(projects, list):
            raise ApiError("GET /api/agent/projects: response has no projects list")
        return projects

    def get_project(self, pid) -> dict:
        return self._request("GET", f"/api/projects/{pid}", auth=False)

    def create_project(self, data: dict, sync: dict) -> dict:
        self._check_fields(data)
        return self._request("POST", "/api/agent/projects", {"project": data, "sync": sync})

    def update_project(self, pid, changes: dict, expected_version: Optional[int], sync: dict) -> dict:
        self._check_fields(changes)
        return self._request("PATCH", f"/api/agent/projects/{pid}",
                             {"changes": changes, "expected_version": expected_version, "sync": sync})

    def save_sync_run(self, run: dict) -> None:
        self._request("POST", "/api/agent/sync-runs", run)
