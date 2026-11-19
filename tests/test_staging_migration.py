"""Safe one-time Railway staging migration (server/migrate.js).

Granular scenarios (success, idempotent no-op, corrupted source, flag-unset zero-touch, stale
lock exclusion, locked-target refusal) are exercised directly against the module in
tests/js/migrate_unit.js - that script needs no running server (store.js's require-time side
effects would otherwise get in the way). This file additionally boots the real server end-to-end
to prove the hook is actually wired into server/index.js ahead of ./seed.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "tests"))
from conftest import ShturmanServer  # noqa: E402


def test_migrate_unit_scenarios(tmp_path):
    r = subprocess.run(["node", "tests/js/migrate_unit.js", str(tmp_path)], cwd=ROOT,
                        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    results = json.loads(r.stdout.strip().splitlines()[-1])
    assert results == {
        "flag_unset_zero_touch": True,
        "success": True,
        "idempotent": True,
        "stale_lock_excluded": True,
        "corrupted_source_safe_fail": True,
        "missing_sections_safe_fail": True,
        "refuses_while_locked": True,
    }


def _empty_shturman(projects=None):
    return {
        "meta": {"format": "shturman-json", "schema_version": 1},
        "stages": [], "docs": [], "projects": projects or [], "project_activities": [],
        "project_history": [], "project_files": [], "agent_events": [], "review_queue": [],
        "sync_runs": [], "plan_imports": [], "plan_source_versions": [], "plan_source_events": [],
        "plan_source_runs": [], "agent_jobs": [], "plan_source": {}, "agent_status": {},
    }


def _build_staging(root: Path, projects=None):
    data_dir = root / "extracted" / "data"
    uploads_dir = root / "extracted" / "uploads"
    data_dir.mkdir(parents=True)
    uploads_dir.mkdir(parents=True)
    source_json = data_dir / "shturman.json"
    source_json.write_text(json.dumps(_empty_shturman(projects)))
    (uploads_dir / "photo.jpg").write_text("upload-bytes")
    return source_json


def test_server_boots_with_migrated_data_end_to_end(tmp_path):
    """The flag triggers the hook ahead of ./seed, and the normal server then starts and serves
    the migrated data - proving server/index.js wiring, not just the migrate.js module."""
    source_json = _build_staging(tmp_path / "staging", projects=[{"id": "e2e-marker", "name": "E2E project"}])

    srv = ShturmanServer(tmp_path / "srv", extra_env={
        "RAILWAY_MIGRATE_FROM_STAGING": "1",
        "MIGRATION_STAGING_SOURCE_JSON": str(source_json),
    })
    srv.start()
    try:
        data = json.loads(srv.data_file.read_text())
        assert data["projects"][0]["id"] == "e2e-marker"
        marker = srv.data_dir / ".migration-from-staging-complete.json"
        assert marker.exists()
        assert (srv.uploads / "photo.jpg").read_text() == "upload-bytes"
    finally:
        srv.stop()

    # restart with the flag still set: idempotent - stays on the migrated data, no crash, no re-run.
    srv2 = ShturmanServer(tmp_path / "srv", extra_env={
        "RAILWAY_MIGRATE_FROM_STAGING": "1",
        "MIGRATION_STAGING_SOURCE_JSON": str(source_json),
    })
    srv2.start()  # same base dir as srv, so same data_file/data_dir/uploads paths
    try:
        data = json.loads(srv2.data_file.read_text())
        assert data["projects"][0]["id"] == "e2e-marker"
    finally:
        srv2.stop()


def test_server_boots_normally_with_flag_unset(tmp_path):
    """Default (flag unset) boot must be completely unaffected, even with no staging source."""
    srv = ShturmanServer(tmp_path / "srv")
    srv.start()
    try:
        assert not (srv.data_dir / ".migration-from-staging-complete.json").exists()
    finally:
        srv.stop()


def test_server_refuses_to_start_on_corrupted_staging_source(tmp_path):
    """A bad staging source must fail the boot loudly rather than start the server half-migrated."""
    data_dir = tmp_path / "srv" / "data"
    data_dir.mkdir(parents=True)
    bad_source = tmp_path / "bad-shturman.json"
    bad_source.write_text("{ not json")

    srv = ShturmanServer(tmp_path / "srv", extra_env={
        "RAILWAY_MIGRATE_FROM_STAGING": "1",
        "MIGRATION_STAGING_SOURCE_JSON": str(bad_source),
    })
    with pytest.raises(RuntimeError):
        srv.start()
    assert srv.proc.returncode != 0
    assert not (data_dir / "shturman.json").exists()
