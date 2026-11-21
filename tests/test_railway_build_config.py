"""Railway build/start configuration (railway.json, railpack.json, nixpacks.toml, Procfile).

Root cause of the incident this guards against: Railway's current builder is Railpack (the
successor to Nixpacks), which auto-detects the app's language by trying a fixed, ordered list of
providers and using the first one whose Detect() matches - and in that list, Python is checked
before Node (see railwayapp/railpack core/providers/provider.go's GetLanguageProviders()). This
repo has requirements.txt at the root (for the unrelated plan_agent Python package, built and run
entirely on the Windows laptop - see plan_agent/windows_agent.py), which the Python provider
detects on its own (core/providers/python/python.go's Detect(): true if requirements.txt exists),
regardless of package.json also being present. Without an explicit override, Railpack therefore
built a Python-only image with no Node/npm installed, and the configured `npm start` start command
then failed with "npm: command not found" even though the deploy itself reported successful.

railpack.json's root `"provider": "node"` is the fix: it forces the Node provider regardless of
requirements.txt, the same role nixpacks.toml's `[phases.setup] nixPkgs = ["nodejs_20"]` played for
the old Nixpacks builder (which nixpacks.toml itself notes is now inactive - Railpack does not read
nixpacks.toml at all). This file is the regression test for that pin, plus the more basic
consistency checks (start command, health check path) across every config file Railway might read.
"""
import json

from conftest import ROOT, ShturmanServer


def test_requirements_txt_exists_at_root_this_is_the_condition_being_guarded_against():
    """If this ever stops being true (the Python package moves, or requirements.txt is removed),
    the ambiguity this test suite is about goes away too - but railpack.json's explicit provider
    pin is harmless either way, so there is nothing to relax."""
    assert (ROOT / "requirements.txt").exists()
    assert (ROOT / "package.json").exists()


def test_railpack_json_pins_the_node_provider():
    cfg = json.loads((ROOT / "railpack.json").read_text())
    assert cfg["provider"].lower() == "node", (
        "without this, Railpack's own provider order (python before node - see module docstring) "
        "picks Python instead, because requirements.txt also exists at the root")
    assert cfg["deploy"]["startCommand"] == "npm start"


def test_railway_json_builder_matches_the_configured_railpack_json():
    """railway.json's own `build.builder` must say what's actually configured (RAILPACK), not a
    stale NIXPACKS value - a mismatch here is exactly what diverged from the Railway dashboard's
    Builder setting and is what triggered the investigation this fix came out of."""
    cfg = json.loads((ROOT / "railway.json").read_text())
    assert cfg["build"]["builder"] == "RAILPACK"
    assert cfg["deploy"]["startCommand"] == "npm start"
    assert cfg["deploy"]["healthcheckPath"] == "/api/health"


def test_start_command_is_consistent_across_every_config_file_railway_might_read():
    railway_cfg = json.loads((ROOT / "railway.json").read_text())
    railpack_cfg = json.loads((ROOT / "railpack.json").read_text())
    package_json = json.loads((ROOT / "package.json").read_text())
    procfile = (ROOT / "Procfile").read_text()

    assert railway_cfg["deploy"]["startCommand"] == "npm start"
    assert railpack_cfg["deploy"]["startCommand"] == "npm start"
    assert package_json["scripts"]["start"] == "node server/index.js"
    assert "web: npm start" in procfile
    # nixpacks.toml is inactive under the current (Railpack) builder, but if it is ever revived by
    # switching the Builder back, it must not silently disagree with the active configuration
    nixpacks_toml = (ROOT / "nixpacks.toml").read_text()
    assert 'cmd = "npm start"' in nixpacks_toml
    assert 'nixPkgs = ["nodejs_20"]' in nixpacks_toml


def test_server_listens_and_api_health_returns_200(tmp_path):
    """The one thing Railway's healthcheck actually probes - confirms server/index.js (unchanged
    by this fix) still starts and answers /api/health, independent of the build-tool question
    above (that part - whether the build environment even has node/npm - only a real Railway
    build can confirm; see docs/RAILWAY_DEPLOYMENT.md)."""
    import requests

    srv = ShturmanServer(tmp_path / "srv")
    srv.start()
    try:
        r = requests.get(srv.url + "/api/health", timeout=10)
        assert r.status_code == 200
        assert r.json()["ok"] is True
    finally:
        srv.stop()
