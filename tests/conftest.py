import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_fixtures  # noqa: E402
from plan_agent.config import Settings, load_policy, load_vocabulary  # noqa: E402


@pytest.fixture(scope="session")
def vocab():
    return load_vocabulary()


@pytest.fixture(scope="session")
def policy():
    return load_policy()


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("plans")
    make_fixtures.make_wide_xlsx(d / "engineering_plan_v1.xlsx", 1)
    make_fixtures.make_wide_xlsx(d / "engineering_plan_v2.xlsx", 2)
    make_fixtures.make_long_csv(d / "engineering_plan_long.csv")
    return d


class ShturmanServer:
    """The real Штурман Node/Express app on a free port with its own temporary
    database, uploads and plan-import folders."""

    def __init__(self, base: Path, key: str = "secret-test-key", seed_demo: bool = False):
        self.base = base
        self.key = key
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_path = base / "data" / "app.sqlite"
        self.uploads = base / "uploads"
        self.plan_dir = base / "plan-imports"
        self.seed_demo = seed_demo
        self.proc = None
        self.log = base / "server.log"

    def env(self):
        e = dict(os.environ)
        e.update(PORT=str(self.port), DATABASE_PATH=str(self.db_path), UPLOADS_DIR=str(self.uploads),
                 PLAN_IMPORT_DIR=str(self.plan_dir), AGENT_API_KEY=self.key,
                 SEED_DEMO_DATA="true" if self.seed_demo else "false", PLAN_AGENT_PYTHON=sys.executable)
        return e

    def start(self):
        self.base.mkdir(parents=True, exist_ok=True)
        fh = open(self.log, "a")
        self.proc = subprocess.Popen(["node", "server/index.js"], cwd=ROOT, env=self.env(), stdout=fh,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if requests.get(self.url + "/api/health", timeout=1).status_code == 200:
                    return self
            except requests.RequestException:
                time.sleep(0.1)
        raise RuntimeError("Штурман server did not start:\n" + self.log.read_text())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self):
        self.stop()
        return self.start()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def shturman(tmp_path):
    srv = ShturmanServer(tmp_path / "shturman").start()
    yield srv
    srv.stop()


@pytest.fixture
def backend(shturman):
    """Base URL of a running Штурман instance (the agent's only backend)."""
    return shturman.url


@pytest.fixture
def make_agent(tmp_path):
    from plan_agent.service import PlanAgent

    def _make(api_url=None, api_key="secret-test-key", state="state"):
        s = Settings(api_url=api_url, api_key=api_key, state_dir=tmp_path / state,
                     config_dir=ROOT / "config", timeout=5, max_retries=1, require_verified_deployment=False)
        agent = PlanAgent(s)
        if agent.client is not None:
            agent.client._sleep = lambda s: None  # no real backoff waits in tests
        return agent
    return _make
