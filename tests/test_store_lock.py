"""server/storeLock.js - the single-writer lock for server/store.js's data directory, fixed for
Railway's persistent Volume. Incident: after a redeploy, the new container (fresh PID namespace)
found the previous container's lock file still on the Volume and refused to start ("data directory
/data is in use by another Штурман process (pid 15)") even though nothing else was running - some
unrelated process in the new container happened to have that same PID number. See
server/storeLock.js's own header comment for the full mechanism (process start time, read from
/proc, as the identity check that survives a container restart but a bare PID can't).
"""
import json
import os
import signal
import subprocess
import time

from conftest import ROOT, ShturmanServer


def run_unit(tmp_path):
    return subprocess.run(["node", "tests/js/store_lock_unit.js", str(tmp_path / "u")], cwd=ROOT,
                           capture_output=True, text=True)


def test_store_lock_unit_scenarios(tmp_path):
    r = run_unit(tmp_path)
    assert r.returncode == 0, r.stderr
    results = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(results.values()), results


def test_a_crashed_server_never_blocks_a_fresh_start_and_data_is_untouched(tmp_path):
    """Simulates the exact Railway incident: a process dies without running its exit handler
    (SIGKILL, same as a container being torn down mid-run), leaving the lock file behind. A new
    process pointed at the same data directory - standing in for the next container, where PIDs
    start over - must still be able to start, and the data file itself must be exactly as the
    first server left it (requirement: never modify shturman.json, never re-run migration - there
    is nothing to migrate here, this is the same JSON file throughout)."""
    srv = ShturmanServer(tmp_path / "srv")
    srv.start()
    import requests
    requests.post(srv.url + "/api/tasks", json={"title": "до аварийного завершения"}, timeout=10)
    data_before = json.loads(srv.data_file.read_text())
    pid = srv.proc.pid
    os.kill(pid, signal.SIGKILL)   # a real crash, not stop() - the exit/SIGTERM handler never runs
    srv.proc.wait(5)
    assert srv.data_dir.joinpath(".shturman.lock").exists(), "the stale lock must still be there, same as on the Volume"

    srv2 = ShturmanServer(tmp_path / "srv")  # same base -> same data_dir, same lock file
    srv2.start()
    try:
        assert requests.get(srv2.url + "/api/health", timeout=10).status_code == 200
        data_after = json.loads(srv.data_file.read_text())
        # meta.updated_at legitimately bumps on every boot (store.tx()'s own housekeeping calls,
        # e.g. planScheduler's recoverInterrupted() - unrelated to this fix); every actual
        # collection must be byte-for-byte the same, nothing lost or rewritten.
        for key in data_before:
            if key == "meta":
                continue
            assert data_after[key] == data_before[key], f"{key} changed - recovering the lock must not touch data"
        assert [t["title"] for t in requests.get(srv2.url + "/api/tasks", timeout=10).json()] == ["до аварийного завершения"]
    finally:
        srv2.stop()


def test_legacy_plain_pid_lock_already_on_disk_is_also_recovered(tmp_path):
    """The exact file shape already stuck on the real Railway Volume (written by the pre-fix
    code: a bare PID, no start time) - a fresh server pointed at it must still start, without
    touching shturman.json or re-running any migration."""
    srv = ShturmanServer(tmp_path / "srv")
    srv.data_dir.mkdir(parents=True, exist_ok=True)
    data_file = srv.data_dir / "shturman.json"
    payload = {"meta": {"format": "shturman-json", "schema_version": 1},
               # the full set, matching server/seed.js's DEFAULT_STAGES - seedStages() adds any
               # missing one on every boot regardless of this fix, so a partial list here would
               # make an unrelated, pre-existing routine look like data this fix touched
               "stages": [{"key": "scanning", "label": "Сканирование", "sort_order": 1},
                          {"key": "development", "label": "Разработка", "sort_order": 2},
                          {"key": "prototype", "label": "Тестовый образец", "sort_order": 3},
                          {"key": "measurement", "label": "Замер", "sort_order": 4},
                          {"key": "documentation", "label": "Оформление", "sort_order": 5}],
               "docs": [], "projects": [], "project_activities": [], "project_history": [],
               "project_files": [], "agent_events": [], "review_queue": [], "sync_runs": [],
               "plan_imports": [], "plan_source_versions": [], "plan_source_events": [],
               "plan_source_runs": [], "agent_jobs": [], "telegram_updates": [], "telegram_log": []}
    data_file.write_text(json.dumps(payload))
    lock_file = srv.data_dir / ".shturman.lock"
    lock_file.write_text("4")  # legacy format: a bare pid, as the old code always wrote
    old = time.time() - 3600
    os.utime(lock_file, (old, old))
    before = json.loads(data_file.read_text())

    srv.start()
    try:
        import requests
        assert requests.get(srv.url + "/api/health", timeout=10).status_code == 200
        after = json.loads(data_file.read_text())
        for key in before:
            if key == "meta":
                continue
            assert after[key] == before[key], f"{key} changed - recovering a stale legacy lock must not touch data"
    finally:
        srv.stop()


def test_a_genuinely_running_second_server_is_still_refused(tmp_path):
    """Requirement: never let two writers in at once. The first server is genuinely alive (not a
    stale leftover) when the second tries to start against the same data directory - this must
    still fail, exactly as before this fix, and the first server's data must be untouched by the
    refused attempt."""
    srv = ShturmanServer(tmp_path / "srv")
    srv.start()
    try:
        import requests
        requests.post(srv.url + "/api/tasks", json={"title": "задача на первом сервере"}, timeout=10)
        before = srv.data_file.read_text()

        second = subprocess.run(["node", "server/index.js"], cwd=ROOT,
                                 env={**srv.env(), "PORT": "0"}, capture_output=True, text=True, timeout=20)
        assert second.returncode != 0 and "in use by another" in second.stderr
        assert srv.data_file.read_text() == before
    finally:
        srv.stop()
