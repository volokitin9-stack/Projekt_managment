"""Network interruption / partial failure behaviour, with a controllable fake transport."""
import pytest
import requests

from plan_agent.client import AuthError, DashboardClient, NetworkError
from plan_agent.config import Settings
from plan_agent.models import ChangePlan, ChangeType, FieldChange, ProjectRecord
from plan_agent.sync import SyncExecutor


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResp(*step)


def client(script, retries=2):
    s = Settings(api_url="http://x", api_key="k", state_dir=None, config_dir=None, max_retries=retries)
    c = DashboardClient(s, FakeSession(script), sleep=lambda _: None)
    c._schema = {"writable_fields": ["project_name", "external_project_id", "priority"]}
    return c


def test_retries_network_errors_then_succeeds():
    c = client([requests.ConnectionError(), (503, {}), (200, {"projects": []})])
    assert c.list_projects() == []
    assert len(c.s.calls) == 3


def test_gives_up_after_retries():
    c = client([requests.ConnectionError()] * 3)
    with pytest.raises(NetworkError):
        c.list_projects()


def test_auth_error_is_not_retried():
    c = client([(401, {"error": "unauthorized"})])
    with pytest.raises(AuthError):
        c.list_projects()
    assert len(c.s.calls) == 1


def test_key_not_in_error_messages():
    c = client([requests.ConnectionError("boom k k")] * 3)
    with pytest.raises(NetworkError) as e:
        c.list_projects()
    assert "Bearer" not in str(e.value)


def test_refuses_unsupported_fields():
    c = client([])
    with pytest.raises(Exception, match="does not support"):
        c.create_project({"project_name": "x", "photos": []}, {})
    assert c.s.calls == []


def test_retried_create_is_confirmed_not_duplicated():
    # 1st POST: connection drops after the server committed; retry gets 409
    # duplicate; agent verifies the existing record instead of creating another.
    rec = ProjectRecord(external_project_id="E-1", project_name="P")
    c = client([requests.ConnectionError(), (409, {"error": "duplicate", "existing_ids": [7]}),
                (200, {"id": 7, "external_project_id": "E-1", "project_name": "P"})])
    plan = ChangePlan(rec, ChangeType.NEW)
    SyncExecutor(c, {"project_name", "external_project_id"}, "run", "f.xlsx").execute([plan])
    assert plan.result == "ok" and plan.dashboard_id == 7
    posts = [call for call in c.s.calls if call[0] == "POST"]
    assert len(posts) == 2


def test_one_failed_record_does_not_stop_the_others():
    r1 = ProjectRecord(external_project_id="E-1", project_name="A")
    r2 = ProjectRecord(external_project_id="E-2", project_name="B")
    c = client([(422, {"error": "invalid_value", "message": "bad"}), (201, {"id": 2})])
    plans = [ChangePlan(r1, ChangeType.NEW), ChangePlan(r2, ChangeType.NEW)]
    fatal = SyncExecutor(c, {"project_name", "external_project_id"}, "run", "f").execute(plans)
    assert fatal is None
    assert [p.result for p in plans] == ["failed", "ok"]


def test_version_conflict_with_foreign_change_is_not_overwritten():
    rec = ProjectRecord(external_project_id="E-1", project_name="A")
    c = client([(409, {"error": "version_conflict"}), (200, {"id": 1, "priority": "low"})])
    plan = ChangePlan(rec, ChangeType.UPDATE, 1, 3, [FieldChange("priority", "low", "high")])
    SyncExecutor(c, {"priority"}, "run", "f").execute([plan])
    assert plan.result == "failed" and "changed in the dashboard" in plan.error
