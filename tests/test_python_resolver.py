"""The planning agent's Python interpreter: PLAN_PYTHON_EXECUTABLE, project .venv, verified
system Python; diagnostics; manual («Обновить») and scheduled (09:00) runs use the same one.

The integration tests create a REAL virtual environment in a folder whose path contains
spaces. They run on Linux and on Windows; .github/workflows/windows-agent-python.yml runs
them on a Windows runner with a project-local .venv."""
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import requests

import make_fixtures
from conftest import ROOT
from test_schedule import WED, at, make_server, status, tick, update

WINDOWS = sys.platform == "win32"
PLAN = "work_plan_2026.xlsx"


def venv_python(d: Path) -> Path:
    return d / ("Scripts/python.exe" if WINDOWS else "bin/python")


@pytest.fixture(scope="module")
def spaced_venv(tmp_path_factory):
    """A virtual environment under 'Program Files/Штурман env/.venv' (spaces + Cyrillic) that
    sees the packages of the interpreter running the tests and logs every start-up."""
    base = tmp_path_factory.mktemp("venvs") / "Program Files" / "Штурман env" / ".venv"
    try:
        venv.EnvBuilder(system_site_packages=True, with_pip=False).create(base)
    except Exception as exc:                       # pragma: no cover - environment without venv support
        pytest.skip(f"cannot create a virtual environment here: {exc}")
    py = venv_python(base)
    site = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    Path(site).mkdir(parents=True, exist_ok=True)
    # A .pth line starting with "import" runs at every interpreter start (a sitecustomize.py could be
    # shadowed by the distribution's own). It logs which executable started, if the server passed
    # SHTURMAN_PY_LOG through to the child process.
    (Path(site) / "_shturman_log.py").write_text(
        "import os, sys\n"
        "log = os.environ.get('SHTURMAN_PY_LOG')\n"
        "if log:\n"
        "    with open(log, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(sys.executable + '\\n')\n", encoding="utf-8")
    (Path(site) / "_shturman_log.pth").write_text("import _shturman_log\n", encoding="utf-8")
    return py


def put(inbox, variant=1):
    make_fixtures.make_monthly_workbook(inbox / PLAN, variant)


def launches(log: Path):
    if not log.exists():
        return []
    return [line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ------------------------------------------------------------------ resolution order (simulated OS)
def test_resolution_order_unit():
    out = subprocess.run(["node", "tests/js/python_resolver_unit.js"], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert all(r.values()) and len(r) == 9


def test_spawn_never_uses_a_shell():
    src = (ROOT / "server" / "routes" / "planImports.js").read_text(encoding="utf-8")
    assert "shell: false" in src and '"python3"' not in src and "PLAN_AGENT_PYTHON ||" not in src
    assert "exec(" not in src and "execSync(" not in src


# ------------------------------------------------------------------ configured interpreter (real venv)
def test_manual_and_scheduled_runs_use_the_configured_interpreter(tmp_path, spaced_venv):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    log = tmp_path / "py_launch.log"
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE=str(spaced_venv), SHTURMAN_PY_LOG=str(log))
    try:
        st = status(s)["python"]
        assert st["ok"] and st["executable"] == str(spaced_venv) and st["source"] == "PLAN_PYTHON_EXECUTABLE"
        put(inbox, 1)
        r = update(s)                                                    # manual «Обновить»
        assert r["run"]["phases"]["analysis"] == "awaiting_approval", r["ui"]
        a = requests.post(s.url + "/api/plan-source/approve", json={"confirm": True}, timeout=180).json()
        assert a["result"]["status"] == "synced"
        manual = launches(log)
        put(inbox, 5)
        t = tick(s, at(WED))                                             # scheduled 09:00 run
        assert t["outcome"] == "synced", t
        scheduled = launches(log)[len(manual):]
        assert manual and scheduled
        # every agent process (analyze, approve, sync) ran in the configured venv
        assert all(Path(p).parent == spaced_venv.parent for p in manual + scheduled), manual + scheduled
        imps = requests.get(s.url + "/api/plan-imports", timeout=10).json()
        assert {i["id"] for i in imps}
        for i in imps:
            full = requests.get(f"{s.url}/api/plan-imports/{i['id']}", timeout=10).json()
            assert full["summary"]["python"] == str(spaced_venv)
    finally:
        s.stop()


def test_missing_interpreter_gives_a_clear_diagnostic_and_recovers(tmp_path, spaced_venv):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    put(inbox, 1)
    missing = str(tmp_path / "Нет такой папки" / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python"))
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE=missing)
    try:
        st = status(s)["python"]
        assert st["ok"] is False and missing in st["error"]
        r = update(s)
        assert r["ui"]["state"] == "error"
        assert missing in r["ui"]["message"] and "PLAN_PYTHON_EXECUTABLE" in r["ui"]["message"]
        assert "secret-test-key" not in json.dumps(r)                     # no credentials in diagnostics
        assert requests.get(s.url + "/api/projects", timeout=10).json() == []
        # fix the configuration and restart: the same, unchanged file is processed (not stuck as "failed")
        s.extra_env["PLAN_PYTHON_EXECUTABLE"] = str(spaced_venv)
        s.restart()
        r = update(s)
        assert r["run"]["phases"]["analysis"] == "awaiting_approval", r["ui"]
    finally:
        s.stop()


@pytest.mark.skipif(WINDOWS, reason="uses a POSIX shell script to imitate the Store alias")
def test_store_alias_like_failure_is_reported(tmp_path):
    fake = tmp_path / "Microsoft Store alias" / "python"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'Python was not found; run without arguments to install from the "
                    "Microsoft Store, or disable this shortcut from Settings' >&2\nexit 9009\n")
    fake.chmod(0o755)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    put(inbox, 1)
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE=str(fake))
    try:
        r = update(s)
        assert r["ui"]["state"] == "error"
        msg = r["ui"]["message"]
        assert str(fake) in msg and "Microsoft Store" in msg and "Не удалось запустить Python-агент" in msg
    finally:
        s.stop()


@pytest.mark.skipif(WINDOWS, reason="file permissions")
def test_unstartable_executable_reports_the_error_code(tmp_path):
    bad = tmp_path / "py dir" / "python"
    bad.parent.mkdir()
    bad.write_text("not a program")                                       # exists, not executable
    bad.chmod(0o644)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    put(inbox, 1)
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE=str(bad))
    try:
        msg = update(s)["ui"]["message"]
        assert str(bad) in msg and "EACCES" in msg
    finally:
        s.stop()


# ------------------------------------------------------------------ automatic detection
def test_project_venv_or_verified_system_python_without_configuration(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE="")
    try:
        st = status(s)["python"]
        assert st["ok"], st
        project_venv = venv_python(ROOT / ".venv")
        if project_venv.exists():                                         # e.g. the Windows CI job / user's PC
            assert st["source"] == "project .venv" and same_file(st["executable"], project_venv)
        else:
            assert st["source"].startswith("system")
            assert "WindowsApps" not in st["executable"]
    finally:
        s.stop()


@pytest.mark.skipif(not WINDOWS, reason="Windows only: real project-local .venv\\Scripts\\python.exe")
def test_windows_project_venv_runs_the_agent(tmp_path):
    project_venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if not project_venv.exists():
        pytest.skip("no project-local .venv on this Windows machine")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    put(inbox, 1)
    s = make_server(tmp_path, inbox, PLAN_PYTHON_EXECUTABLE="")
    try:
        r = update(s)
        assert r["run"]["phases"]["analysis"] == "awaiting_approval", r["ui"]
        imp = requests.get(s.url + "/api/plan-imports", timeout=10).json()[0]
        full = requests.get(f"{s.url}/api/plan-imports/{imp['id']}", timeout=10).json()
        assert same_file(full["summary"]["python"], project_venv)
    finally:
        s.stop()
