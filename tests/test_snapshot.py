"""Reading the shared workbook while colleagues keep it open in Excel.

Synthetic workbooks on the local filesystem only: the Excel owner file (~$...) is
created by the test, and Windows sharing violations are simulated (EBUSY). This
is not a live SMB / Excel test."""
import hashlib
import json
import shutil
import subprocess

import pytest
import requests

import make_fixtures
from conftest import ROOT
from test_schedule import WED, SAT, at, imports, make_server, projects, runs, status, tick, update

NAME = "AfM СIS+OEM 2026.xlsx"            # exact corporate name, Cyrillic «С» in «СIS»
OWNER = "~$" + NAME


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(inbox, variant=1):
    """Save the way Excel does: write a temporary file, then replace the workbook."""
    tmp = inbox / "8F2A1C3D.tmp"
    make_fixtures.make_monthly_workbook(tmp, variant)
    shutil.move(tmp, inbox / NAME)


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "s017"
    d.mkdir()
    return d


@pytest.fixture
def srv(tmp_path, inbox):
    s = make_server(tmp_path, inbox, PLAN_SOURCE_PATH=str(inbox / "AfM СIS+OEM 2026"))   # without extension
    yield s
    s.stop()


def open_in_excel(inbox, who="Иванов И.И."):
    (inbox / OWNER).write_text(who, encoding="utf-8")


def first_sync(s):
    r = update(s)
    assert r["ui"]["state"] == "review_required", r["ui"]
    a = requests.post(s.url + "/api/plan-source/approve", json={"confirm": True}, timeout=180).json()
    assert a["result"]["status"] == "synced", a


# ------------------------------------------------------------------ adapter level (1-10, 13)
def test_snapshot_procedure_unit(tmp_path):
    a, b = tmp_path / "A.xlsx", tmp_path / "B.xlsx"
    make_fixtures.make_monthly_workbook(a, 1)
    make_fixtures.make_monthly_workbook(b, 5)
    out = subprocess.run(["node", "tests/js/snapshot_unit.js", str(tmp_path / "w"), str(a), str(b)], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    for k in ("closed", "open_with_owner_file", "open_unchanged", "open_updated", "owner_file_toggling",
              "modified_during_copy_then_retry", "keeps_changing", "sharing_violation", "corrupted_snapshot",
              "settle", "many_users", "source_preserved"):
        assert r.get(k) is True, k
    assert r["many_users_detail"]["distinct_versions"] == 2


# ------------------------------------------------------------------ manual update (11)
def test_manual_update_while_workbook_is_open(srv, inbox):
    save(inbox, 1)
    open_in_excel(inbox)
    first_sync(srv)
    n = len(projects(srv))
    st = status(srv)
    assert st["last_file"]["name"] == NAME and st["last_file"]["open_in_excel"] is True
    # still open, nothing saved: no changes
    r = update(srv)
    assert r["ui"]["state"] == "no_changes" and r["ui"]["message"].startswith("Изменений нет")
    assert "проверка файла выполнена" in r["ui"]["message"]
    assert r["run"]["phases"]["snapshot"]["open_in_excel"] is True
    # a colleague saves new contents while the workbook stays open: processed
    save(inbox, 5)
    r = update(srv)
    assert r["ui"]["state"] == "updated", r["ui"]
    assert r["ui"]["message"].startswith("Проверка файла выполнена. Файл обработан")
    assert len(projects(srv)) == n
    assert (inbox / OWNER).read_text(encoding="utf-8") == "Иванов И.И."     # owner file untouched


def test_owner_file_appearing_or_disappearing_is_not_a_change(srv, inbox):
    save(inbox, 1)
    first_sync(srv)
    n_imports = len(imports(srv))
    for step in ("open", "close", "open", "other user"):
        if step == "close":
            (inbox / OWNER).unlink()
        else:
            open_in_excel(inbox, "Петров П.П." if step == "other user" else "Иванов И.И.")
        r = update(srv)
        assert r["ui"]["state"] == "no_changes", (step, r["ui"])
    assert len(imports(srv)) == n_imports


# ------------------------------------------------------------------ scheduled update (12)
def test_scheduled_update_while_workbook_is_open(srv, inbox):
    save(inbox, 1)
    first_sync(srv)
    open_in_excel(inbox)
    save(inbox, 5)
    r = tick(srv, at(WED))
    assert r["action"] == "ran" and r["outcome"] == "synced", r
    rec = runs(srv, "schedule")[0]
    assert rec["phases"]["check"] == "ok" and rec["phases"]["sync"] == "ok"
    assert rec["phases"]["snapshot"]["open_in_excel"] is True
    assert tick(srv, at(SAT))["action"] == "non_working"


def test_workbook_saved_during_check_is_postponed_not_failed(tmp_path, inbox):
    save(inbox, 1)
    open_in_excel(inbox)
    # "just saved": younger than the settle time on every attempt -> postponed with a clear status
    s = make_server(tmp_path, inbox, PLAN_SOURCE_PATH=str(inbox / NAME), PLAN_SOURCE_SETTLE_SECONDS="3600",
                    PLAN_SOURCE_READ_ATTEMPTS="2")
    try:
        r = update(s)
        assert r["ui"]["state"] == "retry"
        assert r["ui"]["message"].startswith("Файл изменяется, повторная попытка")
        assert r["run"]["error_code"] == "unstable" and r["run"]["phases"]["check"] == "failed"
        assert imports(s) == [] and projects(s) == []
        assert not requests.get(s.url + "/api/review-queue?kind=plan_source_issue").json()   # transient: no review item
    finally:
        s.stop()


def test_unreadable_saved_version_is_reported(tmp_path, inbox):
    good = tmp_path / "good.xlsx"
    make_fixtures.make_monthly_workbook(good, 1)
    (inbox / NAME).write_bytes(good.read_bytes()[: good.stat().st_size // 2])   # damaged saved version
    open_in_excel(inbox)
    s = make_server(tmp_path, inbox, PLAN_SOURCE_PATH=str(inbox / NAME))
    try:
        r = update(s)
        assert r["ui"]["state"] == "error"
        assert r["ui"]["message"].startswith("Не удалось прочитать сохранённую версию Excel")
        assert imports(s) == [] and projects(s) == []
    finally:
        s.stop()


# ------------------------------------------------------------------ preservation (13)
def test_original_workbook_is_never_modified(srv, inbox):
    save(inbox, 1)
    open_in_excel(inbox)
    wb = inbox / NAME
    before = (sha(wb), wb.stat().st_mtime_ns, wb.stat().st_size)
    owner_before = (inbox / OWNER).read_bytes()
    first_sync(srv)
    update(srv)
    tick(srv, at(WED))
    requests.post(srv.url + "/api/plan-source/check", json={"dry_run": True}, timeout=180)
    assert (sha(wb), wb.stat().st_mtime_ns, wb.stat().st_size) == before
    assert (inbox / OWNER).read_bytes() == owner_before
    assert sorted(p.name for p in inbox.iterdir()) == sorted([NAME, OWNER])   # no files left in the share
