"""Integration tests of the unified Штурман application (Node app + planning agent).

All data is synthetic. The real server is started per test (see conftest.ShturmanServer).
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import make_fixtures
from conftest import ROOT, ShturmanServer

KEY = "secret-test-key"
H = {"Authorization": f"Bearer {KEY}"}
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def create(url, **fields):
    body = {"manufacturer": "TEST", "vehicle_model": "M1", "product_type": "Защита картера", **fields}
    r = requests.post(url + "/api/agent/projects", headers=H, json={"project": body})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- application & git layout
def test_both_systems_live_in_one_repository():
    for f in ("server/index.js", "server/migrations.js", "public/index.html", "plan_agent/service.py",
              "config/profiles/monthly_work_plan.yaml", "package.json"):
        assert (ROOT / f).exists(), f
    assert not (ROOT / "backend").exists()                                  # no second backend/database


def test_dashboard_page_and_api_run(shturman):
    page = requests.get(shturman.url + "/")
    assert page.status_code == 200 and "<title>Штурман</title>" in page.text
    for label in ("Сканирование", "Импорт плана", "Этап не подтверждён", "Трудозатраты", "История проекта",
                  "Фотографии", "План, ч", "Факт, ч", "На стадии", "Не назначен"):
        assert label in page.text, label
    h = requests.get(shturman.url + "/api/health").json()
    assert h["ok"] is True and h["schema_version"] == 6
    stages = requests.get(shturman.url + "/api/stages").json()
    assert [s["key"] for s in stages] == ["scanning", "development", "prototype", "measurement", "documentation"]
    assert [s["label"] for s in stages] == ["Сканирование", "Разработка", "Тестовый образец", "Замер", "Оформление"]


# --------------------------------------------------------------------------- demo data & migrations
def test_demo_projects_only_on_request(tmp_path):
    plain = ShturmanServer(tmp_path / "plain").start()
    try:
        assert requests.get(plain.url + "/api/projects").json() == []
    finally:
        plain.stop()
    demo = ShturmanServer(tmp_path / "demo", seed_demo=True).start()
    try:
        projects = requests.get(demo.url + "/api/projects").json()
        assert len(projects) == 14 and {p["source"] for p in projects} == {"seed"}
    finally:
        demo.stop()


V1_SCHEMA = """
CREATE TABLE docs (collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (collection, id));
CREATE TABLE stages (key TEXT PRIMARY KEY, label TEXT NOT NULL, sort_order INTEGER NOT NULL);
CREATE TABLE projects (id TEXT PRIMARY KEY, external_project_id TEXT, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  needs_review INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE project_files (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'image',
  storage_key TEXT NOT NULL, original_filename TEXT, mime_type TEXT, size INTEGER, is_primary INTEGER NOT NULL DEFAULT 0,
  caption TEXT, source TEXT, telegram_message_id TEXT, telegram_chat_id TEXT, uploaded_by TEXT, uploaded_at TEXT NOT NULL);
CREATE TABLE project_activity (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, action TEXT NOT NULL, source TEXT, actor TEXT,
  previous_value TEXT, new_value TEXT, at TEXT NOT NULL);
CREATE TABLE agent_events (event_id TEXT PRIMARY KEY, project_id TEXT, event_type TEXT, payload TEXT, received_at TEXT NOT NULL);
CREATE TABLE review_queue (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0);
INSERT INTO projects VALUES ('p-real', 'ENG-REAL', '{"manufacturer":"HAVAL","vehicle_model":"Jolion","product_type":"Защита радиатора","priority":"high","status":"development","source":"manual","comments":[{"text":"важно"}]}', 3, 0, '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z');
INSERT INTO docs VALUES ('tasks', 't1', '{"title":"Задача"}', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z');
"""


def test_migration_preserves_existing_data_and_backs_up(tmp_path):
    srv = ShturmanServer(tmp_path / "old")
    srv.db_path.parent.mkdir(parents=True)
    con = sqlite3.connect(srv.db_path)
    con.executescript(V1_SCHEMA)
    con.close()
    srv.start()
    try:
        p = requests.get(srv.url + "/api/projects/p-real").json()
        assert p["external_project_id"] == "ENG-REAL" and p["status"] == "development" and p["priority"] == "high"
        assert p["comments"] == [{"text": "важно"}] and p["version"] == 3
        assert p["stage_entered_at"] is None and p["days_in_stage"] is None   # entry date was never recorded
        assert requests.get(srv.url + "/api/tasks").json()[0]["title"] == "Задача"
        backups = list(srv.db_path.parent.glob("app.sqlite.pre-migration-v2-*.bak"))
        assert len(backups) == 1
        assert sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    finally:
        srv.stop()


# --------------------------------------------------------------------------- stages
def test_unknown_stage_is_never_defaulted(backend):
    p = create(backend, external_project_id="S-1", planned_stage="measurement")
    assert p["status"] is None and p["stage"] is None and p["planned_stage"] == "measurement"
    assert p["priority"] is None                                            # "Не указан", not "normal"
    assert p["days_in_stage"] is None


def test_confirmed_stage_transition_and_duration(backend):
    p = create(backend, external_project_id="S-2")
    r = requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "development", "version": p["version"]})
    assert r.status_code == 200 and r.json()["status"] == "development" and r.json()["days_in_stage"] == 0
    stale = requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "measurement", "version": p["version"]})
    assert stale.status_code == 409                                         # concurrent edit is not overwritten
    bad = requests.post(f"{backend}/api/projects/{p['id']}/stage", json={"stage": "nonsense"})
    assert bad.status_code == 400
    # an explicit agent transition with a verified past timestamp
    ten_days = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    no_confirm = requests.post(f"{backend}/api/agent/projects/S-2/stage-transitions", headers=H,
                               json={"stage": "prototype", "at": ten_days})
    assert no_confirm.status_code == 400
    ok = requests.post(f"{backend}/api/agent/projects/S-2/stage-transitions", headers=H,
                       json={"stage": "prototype", "at": ten_days, "confirmed": True, "note": "опытник готов"})
    assert ok.status_code == 200 and ok.json()["status"] == "prototype" and ok.json()["days_in_stage"] == 10
    hist = requests.get(f"{backend}/api/projects/{p['id']}/history").json()
    trans = [(h["previous_value"], h["new_value"], h["source"]) for h in hist if h["action"] == "stage_transition"]
    assert trans == [(None, "development", "manual"), ("development", "prototype", "agent")]


# --------------------------------------------------------------------------- details: edits, comments, photos
def test_manual_edits_need_version_and_are_recorded(backend):
    p = create(backend, external_project_id="E-1", assigned_engineer="Иванов")
    url = f"{backend}/api/projects/{p['id']}"
    assert requests.patch(url, json={"changes": {"priority": "low"}}).status_code == 400          # version required
    assert requests.patch(url, json={"changes": {"photos": []}, "version": p["version"]}).status_code == 400
    r = requests.patch(url, json={"changes": {"priority": "low", "description": "уточнить крепёж"}, "version": p["version"]})
    assert r.status_code == 200 and r.json()["field_sources"]["priority"] == "manual"
    assert requests.patch(url, json={"changes": {"priority": "high"}, "version": p["version"]}).status_code == 409
    # the agent cannot overwrite the manual value
    upd = requests.patch(f"{backend}/api/agent/projects/E-1", headers=H,
                         json={"changes": {"priority": "high"}, "expected_version": r.json()["version"]})
    assert upd.status_code == 200 and upd.json()["priority"] == "low"
    assert upd.json()["conflicts"][0]["field"] == "priority"


def test_photos_attachments_and_gallery_data(backend):
    p = create(backend, external_project_id="F-1")
    r = requests.post(f"{backend}/api/agent/projects/F-1/images", headers=H, files={"file": ("a.png", PNG, "image/png")},
                      data={"caption": "Примерка", "technical_comment": "зазор 5 мм", "event_at": "2026-11-05T10:00:00Z",
                            "telegram_chat_id": "-100", "telegram_message_id": "42"})
    assert r.status_code == 201 and r.json()["technical_comment"] == "зазор 5 мм"
    again = requests.post(f"{backend}/api/agent/projects/F-1/images", headers=H, files={"file": ("a.png", PNG, "image/png")},
                          data={"telegram_chat_id": "-100", "telegram_message_id": "42"})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    pdf = requests.post(f"{backend}/api/projects/{p['id']}/files", files={"file": ("d.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert pdf.status_code == 201
    bad = requests.post(f"{backend}/api/projects/{p['id']}/files", files={"file": ("x.txt", b"hi", "text/plain")})
    assert bad.status_code == 400
    full = requests.get(f"{backend}/api/projects/{p['id']}").json()
    assert len(full["images"]) == 1 and len(full["attachments"]) == 1
    assert requests.get(backend + full["images"][0]["url"]).content == PNG


def test_telegram_receiver_matches_reliably_or_queues(backend):
    a = create(backend, external_project_id="T-1", article_number="ART-1")
    create(backend, external_project_id="T-2", manufacturer="DUP", vehicle_model="X", product_type="Пороги")
    create(backend, external_project_id="T-3", manufacturer="DUP", vehicle_model="X", product_type="Пороги")
    url = backend + "/api/agent/telegram/photos"

    def send(msg, **hints):
        return requests.post(url, headers=H, files={"file": ("p.png", PNG, "image/png")},
                             data={"telegram_chat_id": "-1", "telegram_message_id": msg, **hints})

    by_article = send("1", article_number="art 1")
    assert by_article.status_code == 201 and by_article.json()["project_id"] == a["id"]
    assert by_article.json()["matched_by"] == "article_number"
    ambiguous = send("2", manufacturer="DUP", vehicle_model="X", product_type="Пороги")
    assert ambiguous.status_code == 202 and "ambiguous" in ambiguous.json()["reason"]
    unknown = send("3", manufacturer="NONE", vehicle_model="Y", product_type="Z")
    assert unknown.status_code == 202
    assert send("3", manufacturer="NONE", vehicle_model="Y", product_type="Z").json()["duplicate"] is True
    queue = requests.get(backend + "/api/review-queue?kind=image").json()
    assert len(queue) == 2
    assert requests.post(url, headers=H, files={"file": ("p.png", PNG, "image/png")}).status_code == 400


# --------------------------------------------------------------------------- Excel import through the dashboard
def test_excel_import_dry_run_approval_and_repeat(shturman, tmp_path):
    wb = tmp_path / "work_plan_2026.xlsx"
    make_fixtures.make_monthly_workbook(wb, 1)
    url = shturman.url + "/api/plan-imports"

    def upload():
        with open(wb, "rb") as fh:
            return requests.post(url, files={"file": ("work_plan_2026.xlsx", fh)}, data={"month": "2026-11"}, timeout=120)

    assert requests.post(url, files={"file": ("x.xlsx", b"")}, data={"month": "2026-11"}).status_code == 400
    first = upload()
    assert first.status_code == 201, first.text
    imp = first.json()
    assert imp["status"] == "analyzed" and imp["report"]["mode"] == "dry_run"
    assert "Ноябрь" in imp["analysis_md"] and imp["report"]["counts"]["new"] > 0
    assert requests.get(shturman.url + "/api/projects").json() == []                 # dry run wrote nothing

    assert requests.post(f"{url}/{imp['id']}/approve", json={}).status_code == 400   # explicit confirmation required
    done = requests.post(f"{url}/{imp['id']}/approve", json={"confirm": True, "approved_by": "pytest"}, timeout=120)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "synced"
    projects = requests.get(shturman.url + "/api/projects").json()
    cats = {p["record_category"] for p in projects}
    assert cats == {"main_project", "additional_work", "backlog"}
    assert all(p["status"] is None for p in projects)                            # no stage confirmed by the import
    gac = next(p for p in projects if p["project_name"] == "GAC S9 Комплект защит")
    assert gac["workload"]["planned_hours"] == 32 and len(gac["activities"]) == 4
    assert {a["source_sheet"] for a in gac["activities"]} == {"Ноябрь"}
    assert requests.get(shturman.url + "/api/review-queue?kind=plan_record").json()

    # the same file again: dry run shows no changes, approval creates no duplicates
    second = upload().json()
    assert second["report"]["counts"]["new"] == 0 and second["report"]["counts"]["updated"] == 0
    requests.post(f"{url}/{second['id']}/approve", json={"confirm": True}, timeout=120)
    assert len(requests.get(shturman.url + "/api/projects").json()) == len(projects)
    assert requests.post(f"{url}/{imp['id']}/approve", json={"confirm": True}).status_code == 409  # already synced


# --------------------------------------------------------------------------- persistence
def test_data_and_images_survive_restart(shturman):
    p = create(shturman.url, external_project_id="R-1")
    requests.post(f"{shturman.url}/api/agent/projects/R-1/images", headers=H, files={"file": ("a.png", PNG, "image/png")})
    requests.post(f"{shturman.url}/api/projects/{p['id']}/stage", json={"stage": "scanning"})
    before = requests.get(f"{shturman.url}/api/projects/{p['id']}").json()
    shturman.restart()
    after = requests.get(f"{shturman.url}/api/projects/{p['id']}").json()
    assert after["status"] == "scanning" and after["stage_entered_at"] == before["stage_entered_at"]
    assert after["history"] == before["history"]
    assert requests.get(shturman.url + after["images"][0]["url"]).content == PNG
