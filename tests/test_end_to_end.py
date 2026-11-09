"""End-to-end: planning agent <-> the real Штурман application over HTTP (generic file layout)."""
import json
import shutil

import openpyxl
import requests

KEY = "secret-test-key"
H = {"Authorization": f"Bearer {KEY}"}
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def projects(url):
    return {p["external_project_id"]: p for p in requests.get(url + "/api/projects").json()}


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
    runs = requests.get(backend + "/api/sync-runs").json()
    assert runs and runs[0]["mode"] == "dry_run"                         # the dry run is recorded, nothing else


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
    p1 = db["ENG-001"]
    assert p1["planned_stage"] == "prototype"                      # suggestion from planned work ...
    assert p1["status"] is None and p1["days_in_stage"] is None     # ... never a confirmed Kanban stage
    assert p1["priority"] == "high" and p1["priority_original"] is None
    assert db["ENG-002"]["priority"] == "normal"                   # medium -> Штурман "normal"
    assert db["ENG-004"]["work_status"] is None                    # unknown stays unknown
    assert db["ENG-005"]["priority"] is None                       # not "normal" by default

    again = agent.receive(fixtures_dir / "engineering_plan_v1.xlsx", "watch_folder")
    assert (again.report["counts"]["new"], again.report["counts"]["updated"]) == (0, 0)
    assert again.report["counts"]["unchanged"] == 5
    assert len(projects(backend)) == 5

    # manual dashboard work that must survive
    pid, ver = p1["id"], p1["version"]
    r = requests.patch(f"{backend}/api/projects/{pid}", json={"changes": {"assigned_engineer": "Морозов"}, "version": ver})
    assert r.status_code == 200, r.text
    assert requests.post(f"{backend}/api/projects/{pid}/comments", json={"text": "Клиент просил ускорить"}).status_code == 201
    up = requests.post(f"{backend}/api/projects/{pid}/files", files={"file": ("p.png", PNG, "image/png")},
                       data={"caption": "Опытный образец"})
    assert up.status_code == 201, up.text

    v2 = agent.receive(fixtures_dir / "engineering_plan_v2.xlsx", "api")
    c = v2.report["counts"]
    assert v2.status == "Completed", v2.text
    assert c["new"] == 1 and c["updated"] == 1 and c["sync_errors"] == 0
    db = projects(backend)
    assert "ENG-002" in db                                          # never deleted
    p1 = requests.get(f"{backend}/api/projects/{pid}").json()
    assert p1["planned_stage"] == "measurement" and p1["planned_deadline"] == "2026-11-30"
    assert p1["assigned_engineer"] == "Морозов"                    # manual edit kept
    assert [x["text"] for x in p1["comments"]] == ["Клиент просил ускорить"]
    assert len(p1["images"]) == 1 and p1["images"][0]["caption"] == "Опытный образец"
    conflicts = [cf for p in v2.report["projects"] for cf in p["conflicts"]]
    assert any(cf["field"] == "assigned_engineer" and cf["resolution"] == "kept_dashboard" for cf in conflicts)

    hist = p1["history"]
    acts = {(h["action"], h["source"]) for h in hist}
    assert ("updated:assigned_engineer", "manual") in acts
    assert ("updated:planned_stage", "agent") in acts
    stage = next(h for h in hist if h["action"] == "updated:planned_stage")
    assert (stage["previous_value"], stage["new_value"]) == ("prototype", "measurement")
    assert any(h["action"] == "agent_sync" and h["new_value"]["sync_run_id"] == v2.run_id for h in hist)

    runs = requests.get(f"{backend}/api/agent/sync-runs", headers=H).json()["sync_runs"]
    assert {r["id"] for r in runs} >= {out.run_id, v2.run_id}
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
    wb["План КО"]["U4"] = "Ответственный за КД"         # new column
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


def test_agent_api_rejects_invalid_requests(backend):
    url = backend + "/api/agent/projects"
    base = {"manufacturer": "TEST", "vehicle_model": "T1", "product_type": "probe"}
    r = requests.post(url, headers=H, json={"project": {**base, "photos": ["a.jpg"]}})
    assert r.status_code == 400 and r.json()["code"] == "unsupported_fields"          # never silently dropped
    r = requests.post(url, headers=H, json={"project": {**base, "priority": "urgent!!"}})
    assert r.status_code == 400
    r = requests.post(url, headers=H, json={"project": {**base, "status": "development"}})
    assert r.status_code == 400                                                       # agent cannot confirm a stage
    r = requests.post(url, headers=H, json={"project": {"product_type": "no vehicle"}})
    assert r.status_code == 400                                                       # main project needs a vehicle
    assert requests.post(url, json={"project": base}).status_code == 401
    ok = requests.post(url, headers=H, json={"project": {**base, "external_project_id": "Q-1"}})
    assert ok.status_code == 201
    dup = requests.post(url, headers=H, json={"project": {**base, "external_project_id": "Q-1"}, "create_only": True})
    assert dup.status_code == 409 and dup.json()["existing_ids"] == [ok.json()["id"]]
    stale = requests.patch(f"{url}/{ok.json()['id']}", headers=H, json={"changes": {"priority": "low"}, "expected_version": 99})
    assert stale.status_code == 409 and stale.json()["error"] == "version_conflict"
    bad_act = requests.patch(f"{url}/Q-1", headers=H, json={"changes": {}, "activities": [{"activity_key": "x"}]})
    assert bad_act.status_code == 400                                                 # activities need a plan month
    aw = requests.post(url, headers=H, json={"project": {"record_category": "additional_work",
                                                         "project_name": "Техподдержка", "external_project_id": "AW-1"}})
    assert aw.status_code == 201 and aw.json()["record_category"] == "additional_work"


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
