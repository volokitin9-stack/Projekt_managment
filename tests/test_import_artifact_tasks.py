"""scripts/import_artifact_tasks.js - idempotent one-time import of the personal Kanban board's
"tasks" collection, exported from the Claude Artifact ("Экспорт задач в JSON" button), into this
app's own "tasks" doc store. See docs/TELEGRAM_INTEGRATION.md "Stage 4 - data migration".

Synthetic export data only - no real board content.
"""
import json
import subprocess
from pathlib import Path

import requests

from conftest import ROOT, ShturmanServer


def run(args, env):
    return subprocess.run(["node", "scripts/import_artifact_tasks.js"] + args, cwd=ROOT, env=env,
                           capture_output=True, text=True)


def export_file(tmp_path, tasks):
    p = tmp_path / "export.json"
    p.write_text(json.dumps({
        "export_meta": {"exported_at": "2026-01-01T00:00:00Z",
                         "source": {"type": "Claude Artifact (ArtifactData)", "url": "https://claude.ai/artifact/x",
                                    "collection": "tasks"},
                         "record_count": len(tasks)},
        "tasks": tasks
    }), encoding="utf-8")
    return p


def test_dry_run_writes_nothing_then_write_imports_and_is_idempotent(tmp_path):
    srv = ShturmanServer(tmp_path / "srv")
    env = srv.env()
    src = export_file(tmp_path, [
        {"id": "a1", "title": "Задача раз", "quadrant": "inbox", "done": False, "direction": "E-com",
         "responsible": "Иванов", "from": "самостоятельно", "due": "2026-02-01", "source": "telegram",
         "telegramNotified": True, "calendarSynced": False,
         "created_at": "2026-01-05T10:00:00.000Z", "updated_at": "2026-01-06T11:00:00.000Z",
         "subtasks": [{"id": "s1", "title": "Шаг 1", "responsible": "Петров", "due": None, "done": False,
                       "createdAt": "2026-01-05T10:01:00.000Z"}]},
        {"id": "a2", "title": "Задача два", "quadrant": "do", "done": True, "direction": None,
         "responsible": None, "from": None, "due": None, "telegramNotified": False, "calendarSynced": False,
         "created_at": "2026-01-02T00:00:00.000Z", "updated_at": "2026-01-02T00:00:00.000Z"},
    ])

    dry = run([str(src)], env)
    assert dry.returncode == 0 and "Would import 2" in dry.stdout
    # requiring server/store.js itself seeds an empty shturman.json (pre-existing behaviour,
    # unrelated to this script) - what matters for a dry run is that no task was actually created
    if srv.data_file.exists():
        assert json.loads(srv.data_file.read_text())["docs"] == []

    done = run([str(src), "--write"], env)
    assert done.returncode == 0 and "Imported 2 new task(s); skipped 0" in done.stdout

    srv.start()
    try:
        tasks = requests.get(srv.url + "/api/tasks", timeout=10).json()
        by_id = {t["id"]: t for t in tasks}
        assert set(by_id) == {"a1", "a2"}
        t1 = by_id["a1"]
        assert t1["title"] == "Задача раз" and t1["quadrant"] == "inbox" and t1["responsible"] == "Иванов"
        assert t1["due"] == "2026-02-01" and t1["source"] == "telegram"
        assert t1["subtasks"] == [{"id": "s1", "title": "Шаг 1", "responsible": "Петров", "due": None,
                                    "done": False, "createdAt": "2026-01-05T10:01:00.000Z"}]
        # the export's real dates are preserved under their own fields, not silently lost
        assert t1["importedCreatedAt"] == "2026-01-05T10:00:00.000Z"
        assert t1["importedUpdatedAt"] == "2026-01-06T11:00:00.000Z"
        assert by_id["a2"]["done"] is True
    finally:
        srv.stop()

    # re-running the same export (e.g. accidentally twice) must never duplicate anything
    again = run([str(src), "--write"], env)
    assert again.returncode == 0 and "Imported 0 new task(s); skipped 2" in again.stdout
    srv.start()
    try:
        tasks = requests.get(srv.url + "/api/tasks", timeout=10).json()
        assert len(tasks) == 2
    finally:
        srv.stop()


def test_a_later_export_with_new_tasks_only_adds_the_new_ones(tmp_path):
    """The real workflow: export today, import; someone keeps using the board; export again next
    week (now including new tasks since then) and import again - existing records must not be
    touched a second time, only the genuinely new ones get added."""
    srv = ShturmanServer(tmp_path / "srv")
    env = srv.env()
    first = export_file(tmp_path, [{"id": "b1", "title": "Первая", "quadrant": "inbox", "done": False,
                                     "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}])
    assert run([str(first), "--write"], env).returncode == 0

    second = export_file(tmp_path, [
        {"id": "b1", "title": "Первая (изменена бы в артефакте, но здесь не перезаписывается)", "quadrant": "do",
         "done": True, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-10T00:00:00Z"},
        {"id": "b2", "title": "Вторая, новая", "quadrant": "plan", "done": False,
         "created_at": "2026-01-09T00:00:00Z", "updated_at": "2026-01-09T00:00:00Z"},
    ])
    result = run([str(second), "--write"], env)
    assert result.returncode == 0 and "Imported 1 new task(s); skipped 1" in result.stdout and "b1" in result.stdout

    srv.start()
    try:
        tasks = {t["id"]: t for t in requests.get(srv.url + "/api/tasks", timeout=10).json()}
        assert set(tasks) == {"b1", "b2"}
        assert tasks["b1"]["title"] == "Первая" and tasks["b1"]["quadrant"] == "inbox"  # untouched, not overwritten
        assert tasks["b2"]["title"] == "Вторая, новая"
    finally:
        srv.stop()


def test_rejects_a_file_with_no_tasks_array(tmp_path):
    srv = ShturmanServer(tmp_path / "srv")
    bad = tmp_path / "not-an-export.json"
    bad.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    r = run([str(bad)], srv.env())
    assert r.returncode == 2 and "tasks" in r.stderr


# ---------------------------------------------------------- критическое требование №3: pre-import
# analysis, duplicate detection, rollback (ТЗ "Дополнение к ТЗ Telegram-интеграции «Штурмана»")

def test_dry_run_reports_preliminary_analysis(tmp_path):
    srv = ShturmanServer(tmp_path / "srv")
    env = srv.env()
    src = export_file(tmp_path, [
        {"id": "c1", "title": "Новая", "quadrant": "inbox", "done": False},
        {"id": "c2", "title": "Другая", "quadrant": "do", "done": True},
    ])
    out = run([str(src)], env).stdout
    assert "2 record(s)" in out and "inbox=1" in out and "do=1" in out
    assert "1 done, 1 active" in out
    assert "0 existing task(s)" in out
    assert "2 new, 0 duplicate" in out


def test_write_produces_a_rollback_manifest_and_rollback_removes_exactly_those_ids(tmp_path):
    srv = ShturmanServer(tmp_path / "srv")
    env = srv.env()
    src = export_file(tmp_path, [
        {"id": "r1", "title": "Задача 1", "quadrant": "inbox", "done": False},
        {"id": "r2", "title": "Задача 2", "quadrant": "do", "done": False},
    ])
    out = run([str(src), "--write"], env).stdout
    assert "Rollback manifest written" in out
    manifest_path = [line.split(": ", 1)[1].strip() for line in out.splitlines()
                      if line.startswith("Rollback manifest written")][0]
    manifest = json.loads(Path(manifest_path).read_text())
    assert set(manifest["created_ids"]) == {"r1", "r2"}

    srv.start()
    try:
        assert {t["id"] for t in requests.get(srv.url + "/api/tasks", timeout=10).json()} == {"r1", "r2"}
        # a task created directly on the board afterwards must never be touched by a rollback
        requests.post(srv.url + "/api/tasks", json={"id": "manual1", "title": "Создана вручную"}, timeout=10)
    finally:
        srv.stop()

    dry = run(["--rollback", manifest_path], env)
    assert dry.returncode == 0 and "Would remove 2" in dry.stdout and "r1" in dry.stdout and "r2" in dry.stdout
    srv.start()
    try:
        assert {t["id"] for t in requests.get(srv.url + "/api/tasks", timeout=10).json()} == {"r1", "r2", "manual1"}
    finally:
        srv.stop()

    done = run(["--rollback", manifest_path, "--write"], env)
    assert done.returncode == 0 and "Removed 2 task(s)" in done.stdout
    srv.start()
    try:
        ids = {t["id"] for t in requests.get(srv.url + "/api/tasks", timeout=10).json()}
        assert ids == {"manual1"}, "rollback must remove exactly the manifest's own ids, nothing else"
    finally:
        srv.stop()

    # rolling back the same manifest again is a safe no-op, not an error
    again = run(["--rollback", manifest_path, "--write"], env)
    assert again.returncode == 0 and "Removed 0 task(s)" in again.stdout and "already gone" in again.stdout


def test_rollback_rejects_a_file_that_is_not_an_import_manifest(tmp_path):
    srv = ShturmanServer(tmp_path / "srv")
    bad = tmp_path / "not-a-manifest.json"
    bad.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    r = run(["--rollback", str(bad)], srv.env())
    assert r.returncode == 2 and "created_ids" in r.stderr
