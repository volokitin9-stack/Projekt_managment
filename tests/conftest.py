import sys
import threading
from pathlib import Path

import pytest

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


@pytest.fixture
def backend(tmp_path):
    """Real reference backend on a random port, fresh database per test."""
    from backend.server import make_server
    srv = make_server(str(tmp_path / "db.sqlite"), "127.0.0.1", 0, "secret-test-key")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


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
