"""End-to-end: agent <-> real reference backend over HTTP."""
import json
import shutil

import openpyxl
import requests

KEY = "secret-test-key"


def projects(url):
    return {p["external_project_id"]: p for p in requests.get(url + "/api/projects").json()["projects"]}


def approve(agent):
    agent.store.approve("pytest")


def test_initial_mode_is_dry_run_and_writes_nothing(backend, make_agent, fixtures_dir):
    agent = make_agent(backend)
    out = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    assert out.status == "Approval required (dry run)"
    assert out.report["counts"]["new"] == 5 and out.report["counts"]["review"] == 1
    assert out.analysis_markdown and "Initial analysis" in out.analysis_markdown
    assert agent.store.proposed_path.exists()
    assert projects(backend) == {}


def test_full_lifecycle(backend, make_agent, fixtures_dir):
    agent = make_agent(backend)
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)

    out = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    c = out.report["counts"]
    assert out.status == "Completed", out.text
    assert (c["new"], c["updated"], c["review"], c["sync_errors"]) == (5, 0, 1, 0)
    db = projects(backend)
    assert set(db) == {"ENG-001", "ENG-002", "ENG-003", "ENG-004", "ENG-005"}
    assert "ENG-006" not in db                                     # ambiguous record was not written
    assert db["ENG-001"]["current_stage"] == "test_prototype"
    assert db["ENG-004"]["project_status"] is None                 # unknown stays unknown

    # idempotent: same file again -> nothing new, nothing updated
    again = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "watch_folder")
    assert (again.report["counts"]["new"], again.report["counts"]["updated"]) == (0, 0)
    assert again.report["counts"]["unchanged"] == 5
    assert len(projects(backend)) == 5

    # manual dashboard work that must survive
    pid = db["ENG-001"]["id"]
    requests.patch(f"{backend}/api/projects/{pid}",
                   json={"changes": {"assigned_engineer": "Морозов", "comments": "Клиент просил ускорить"}})
    requests.post(f"{backend}/api/projects/{pid}/attachments",
                  json={"kind": "photo", "url": "https://example.invalid/p.jpg", "uploaded_by": "image_agent"})

    # second version: progress, new project, a project missing from the file
    v2 = agent.receive(fixtures_dir / "engineering_plan_v2.xlsx", "api")
    c = v2.report["counts"]
    assert v2.status == "Completed", v2.text
    assert c["new"] == 1 and c["updated"] == 1 and c["sync_errors"] == 0
    db = projects(backend)
    assert "ENG-002" in db                                          # never deleted
    p1 = requests.get(f"{backend}/api/projects/{pid}").json()
    assert p1["current_stage"] == "measurement" and p1["planned_end_date"] == "2026-11-30"
    assert p1["assigned_engineer"] == "Морозов"                    # manual edit kept
    assert p1["comments"] == "Клиент просил ускорить"
    assert len(p1["attachments"]) == 1
    conflicts = [cf for p in v2.report["projects"] for cf in p["conflicts"]]
    assert any(cf["field"] == "assigned_engineer" and cf["resolution"] == "kept_dashboard" for cf in conflicts)

    hist = requests.get(f"{backend}/api/projects/{pid}/history").json()["history"]
    by_source = {(h["field"], h["source_type"]) for h in hist}
    assert ("assigned_engineer", "manual") in by_source
    assert ("current_stage", "agent") in by_source
    stage_change = next(h for h in hist if h["field"] == "current_stage")
    assert (stage_change["previous_value"], stage_change["new_value"]) == ("test_prototype", "measurement")
    assert stage_change["source"] == "engineering_plan_v2.xlsx" and stage_change["sync_run_id"] == v2.run_id

    runs = requests.get(f"{backend}/api/agent/sync-runs", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert {r["id"] for r in runs["sync_runs"]} >= {out.run_id, v2.run_id}
    lines = agent.history_path.read_text().splitlines()
    assert json.loads(lines[-1])["run_id"] == v2.run_id


def test_shifted_identity_goes_to_review(backend, make_agent, fixtures_dir, tmp_path):
    agent = make_agent(backend)
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    wb = openpyxl.load_workbook(fixtures_dir / "engineering_plan_v1.xlsx")
    wb["План КО"]["B9"] = "Jolion"          # ENG-003 suddenly "changes" vehicle
    p = tmp_path / "shifted.xlsx"
    wb.save(p)
    out = agent.receive(p, "cli")
    eng3 = next(x for x in out.report["projects"] if x["source"].endswith("!9"))
    assert eng3["change_type"] == "review"
    assert projects(backend)["ENG-003"]["vehicle_model"] == "Dargo"


def test_structure_change_requires_new_validation(backend, make_agent, fixtures_dir, tmp_path):
    agent = make_agent(backend)
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)
    wb = openpyxl.load_workbook(fixtures_dir / "engineering_plan_v1.xlsx")
    ws = wb["План КО"]
    ws["U4"] = "Ответственный за КД"         # new column
    p = tmp_path / "changed.xlsx"
    wb.save(p)
    out = agent.receive(p, "cli")
    assert out.status == "Approval required (dry run)"
    assert any("STRUCTURE CHANGED" in n for n in out.report["notes"])
    assert projects(backend) == {}


def test_new_value_after_approval_sends_project_to_review(backend, make_agent, fixtures_dir, tmp_path):
    agent = make_agent(backend)
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)
    wb = openpyxl.load_workbook(fixtures_dir / "engineering_plan_v1.xlsx")
    wb["План КО"]["R7"] = "Ждёт решения заказчика"     # status value never approved
    p = tmp_path / "newvalue.xlsx"
    wb.save(p)
    out = agent.receive(p, "cli")
    eng1 = next(x for x in out.report["projects"] if x["source"].endswith("!7"))
    assert eng1["change_type"] == "review"
    assert out.report["counts"]["new"] == 4                  # the other projects still sync


def test_wrong_api_key(backend, make_agent, fixtures_dir):
    agent = make_agent(backend, api_key="wrong")
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)
    out = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    assert out.report["mode"] == "dry_run"
    assert "authentication rejected" in out.report["fatal_error"]
    assert projects(backend) == {}


def test_api_unreachable(make_agent, fixtures_dir):
    agent = make_agent("http://127.0.0.1:9")      # nothing listens there
    agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    approve(agent)
    out = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "cli")
    assert out.report["mode"] == "dry_run" and "network error" in out.report["fatal_error"]
    assert out.report["dashboard_comparison"] is False


def test_unsupported_and_empty_files_fail_cleanly(make_agent, tmp_path):
    agent = make_agent()
    bad = tmp_path / "photo.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    assert agent.receive(bad, "api").status == "Failed"
    empty = tmp_path / "e.csv"
    empty.write_bytes(b"")
    out = agent.receive(empty, "api")
    assert out.status == "Failed" and "empty" in out.report["fatal_error"]


def test_backend_rejects_unsupported_fields_and_bad_values(backend):
    h = {"Authorization": f"Bearer {KEY}"}
    r = requests.post(backend + "/api/agent/projects", headers=h,
                      json={"project": {"project_name": "X", "photos": ["a.jpg"]}})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_fields"
    r = requests.post(backend + "/api/agent/projects", headers=h,
                      json={"project": {"project_name": "X", "priority": "urgent!!"}})
    assert r.status_code == 422
    r = requests.post(backend + "/api/agent/projects", json={"project": {"project_name": "X"}})
    assert r.status_code == 401
    ok = requests.post(backend + "/api/agent/projects", headers=h,
                       json={"project": {"project_name": "X", "external_project_id": "Q-1"}})
    assert ok.status_code == 201
    dup = requests.post(backend + "/api/agent/projects", headers=h,
                        json={"project": {"project_name": "X2", "external_project_id": "Q-1"}})
    assert dup.status_code == 409 and dup.json()["existing_ids"] == [ok.json()["id"]]
    stale = requests.patch(f"{backend}/api/agent/projects/{ok.json()['id']}", headers=h,
                           json={"changes": {"priority": "low"}, "expected_version": 99})
    assert stale.status_code == 409 and stale.json()["error"] == "version_conflict"


def test_reprocess_from_inbox(backend, make_agent, fixtures_dir, tmp_path):
    agent = make_agent(backend)
    src = tmp_path / "Copy of plan (3).xlsx"
    shutil.copy(fixtures_dir / "engineering_plan_v1.xlsx", src)
    first = agent.receive(src, "api", "Copy of plan (3).xlsx")
    approve(agent)
    out = agent.reprocess(first.report["file_sha256"][:12])
    assert out.status == "Completed" and out.report["counts"]["new"] == 5


def test_http_intake(backend, make_agent, fixtures_dir):
    import threading

    from plan_agent.intake import make_intake_server
    agent = make_agent(backend)
    srv = make_intake_server(agent, "127.0.0.1", 0, "intake-key")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/files"
    data = (fixtures_dir / "engineering_plan_v1.xlsx").read_bytes()
    assert requests.post(url, data=data).status_code == 401
    r = requests.post(url, data=data, headers={"Authorization": "Bearer intake-key", "X-Filename": "plan.xlsx",
                                               "X-Sender": "planning-agent"})
    assert r.status_code == 200 and r.json()["status"] == "Approval required (dry run)"
    approve(agent)
    r = requests.post(url, data=data, headers={"Authorization": "Bearer intake-key"})
    assert r.json()["status"] == "Completed" and r.json()["counts"]["new"] == 5
    srv.shutdown()
